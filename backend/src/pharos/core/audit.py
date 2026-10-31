"""The audit log: who did what, when, with what result (THREAT_MODEL, ADR-0011).

Rows are append-only for the application role (INSERT and SELECT only). Two ways to write:

* `record` writes inside the caller's transaction, so a change and its audit row commit or roll back
  together. Use it for successful actions.
* `record_independently` opens its own organization-scoped transaction and commits it, so the row of
  a FAILED attempt survives the rollback of the request that failed. It is best effort: if the audit
  write itself fails, that is logged and the request goes on (an unavailable database fails the
  request anyway).

What is stored is deliberately narrow: an action from a fixed catalogue, an outcome, the actor, a
short target, an HMAC of the client address (never the address) and a few scalar details. Anything
that looks like a secret in the details is refused as a programming error.
"""

import hashlib
import hmac
import json
import logging
import re
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pharos.core.rls import tenant_session

logger = logging.getLogger(__name__)

_DETAIL_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")
_SECRETISH = re.compile(
    r"password|passwd|secret|token|cookie|authorization|credential|api_?key|session|csrf",
    re.IGNORECASE,
)
_MAX_DETAILS = 20
_MAX_TEXT = 200


class AuditAction(StrEnum):
    """Every event that is written to the log. Add new ones here, in the same pull request."""

    LOGIN = "auth.login"
    LOGOUT = "auth.logout"
    SESSIONS_REVOKED = "auth.sessions_revoked"
    LOGIN_UNLOCKED = "auth.login_unlocked"
    USER_CREATED = "user.created"
    USER_ROLE_CHANGED = "user.role_changed"
    USER_STATUS_CHANGED = "user.status_changed"
    PASSWORD_SET = "credential.password_set"  # noqa: S105  (an event name, not a password)
    ORGANIZATION_CREATED = "org.created"
    SOURCE_CREATED = "source.created"
    SOURCE_UPDATED = "source.updated"
    SOURCE_ENABLED = "source.enabled"
    SOURCE_DISABLED = "source.disabled"
    SOURCE_DELETED = "source.deleted"
    SOURCE_TEST_FETCH = "source.test_fetch_requested"
    LLM_KEY_TESTED = "llm.key_test_requested"
    ITEM_HELD = "item.held"
    ITEM_APPROVED = "item.approved"
    ITEM_HIDDEN = "item.hidden"
    ITEM_RESTORED = "item.restored"


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class ActorType(StrEnum):
    USER = "user"
    LOCAL_ADMIN = "local_admin"
    OPERATOR_CLI = "operator_cli"
    SYSTEM = "system"


def clean_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """Scalar values only, short text, no secret-looking keys."""
    if not details:
        return {}
    if len(details) > _MAX_DETAILS:
        raise ValueError("too many audit details")
    cleaned: dict[str, Any] = {}
    for key, value in details.items():
        if not _DETAIL_KEY.fullmatch(key):
            raise ValueError(f"invalid audit detail key: {key!r}")
        if _SECRETISH.search(key):
            raise ValueError(f"audit detail key looks like a secret: {key!r}")
        if isinstance(value, str):
            value = value[:_MAX_TEXT]
        elif value is not None and not isinstance(value, bool | int | float):
            raise ValueError(f"audit detail {key!r} must be a scalar")
        cleaned[key] = value
    return cleaned


def ip_hash(key: bytes, ip: str | None) -> str | None:
    """A keyed hash of the client address: comparable between events, useless without the key."""
    if not ip:
        return None
    return hmac.new(key, ip.encode(), hashlib.sha256).hexdigest()


class AuditLog:
    def __init__(self, factory: async_sessionmaker[AsyncSession], ip_key: bytes) -> None:
        self._factory = factory
        self._ip_key = ip_key

    def _values(
        self,
        org_id: uuid.UUID,
        action: AuditAction,
        outcome: Outcome,
        actor_type: ActorType,
        actor_id: uuid.UUID | None,
        target: str | None,
        ip: str | None,
        details: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "org_id": org_id,
            "actor_type": actor_type.value,
            "actor_id": actor_id,
            "action": action.value,
            "target": target[:_MAX_TEXT] if target else None,
            "outcome": outcome.value,
            "ip_hash": ip_hash(self._ip_key, ip),
            "details": json.dumps(clean_details(details)),
        }

    _INSERT = text(
        "INSERT INTO audit_logs (org_id, actor_type, actor_id, action, target, outcome, ip_hash, "
        "details) VALUES (:org_id, :actor_type, :actor_id, :action, :target, :outcome, :ip_hash, "
        "CAST(:details AS jsonb))"
    )

    async def record(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        action: AuditAction,
        outcome: Outcome = Outcome.SUCCESS,
        actor_type: ActorType = ActorType.USER,
        actor_id: uuid.UUID | None = None,
        target: str | None = None,
        ip: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Write inside the caller's transaction (committed or rolled back with it)."""
        values = self._values(org_id, action, outcome, actor_type, actor_id, target, ip, details)
        await session.execute(self._INSERT, values)

    async def record_independently(
        self,
        *,
        org_id: uuid.UUID,
        action: AuditAction,
        outcome: Outcome = Outcome.FAILURE,
        actor_type: ActorType = ActorType.SYSTEM,
        actor_id: uuid.UUID | None = None,
        target: str | None = None,
        ip: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Write and commit in a transaction of its own. Returns False if the write failed."""
        values = self._values(org_id, action, outcome, actor_type, actor_id, target, ip, details)
        try:
            async with tenant_session(self._factory, org_id) as session:
                await session.execute(self._INSERT, values)
        except Exception:
            logger.exception("audit: could not write the audit row", extra={"action": action.value})
            return False
        return True
