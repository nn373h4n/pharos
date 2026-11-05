"""The approved AI tools (M5-07, ADR-0026): what administrators change and everybody reads.

* Reading is for everybody who is signed in: the "Try this" panel of a card shows a link to each
  tool. Creating, changing and deleting is for administrators only.
* The server never opens an address: the person's browser does. What is checked here is that an
  address is something a browser may safely be sent to: `https` only, no credentials, no fragment,
  no white space or control characters, an ASCII host name (write an international name in its
  punycode form) and not a bare IP address. An internal host name is allowed on purpose: an
  organization may run its own AI portal.
* A refusal that matters (the same name twice, the cap) leaves a failure row in the audit log,
  written on its own because the request's transaction is rolled back. Audit entries name the tool
  and what changed; they never contain the address or the name (free text).
"""

import ipaddress
import logging
import re
import unicodedata
import uuid
from typing import Any, NoReturn
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError

from pharos.aitools import repository
from pharos.aitools.schemas import NAME_LIMIT, URL_LIMIT, AiToolIn, AiToolPatch
from pharos.core.audit import ActorType, AuditAction, AuditLog, Outcome
from pharos.core.errors import ApiError, ErrorCode, not_found
from pharos.core.principal import Principal
from pharos.core.unit_of_work import UnitOfWork
from pharos.ingest.text import clean_text

logger = logging.getLogger(__name__)

MAX_ORG_TOOLS = 10

# Control (Cc), format (Cf: zero-width and bidirectional controls), surrogate, private use,
# unassigned (Cn) and separator (Z*: spaces, the line and paragraph separators) characters have no
# place in an address a person copies or clicks: they hide where it goes.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zs", "Zl", "Zp"})
# A browser reads a host as an IPv4 address when its LAST label is a number, in decimal, octal or
# hexadecimal ("2130706433", "0x7f.1", "foo.127"): "127.0.0.1" however it is written. A real host
# name such as "123.example.com" ends in a name and is fine.
_NUMBER_LABEL = re.compile(r"^(0[xX][0-9a-fA-F]*|[0-9]+)$")
_HOST_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _invalid(field: str, kind: str) -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_FAILED, 422, errors=[{"loc": ["body", field], "type": kind}]
    )


def clean_name(name: str) -> str:
    """The name as plain text. A name that changes when cleaned (markup, control or invisible
    characters) is refused, not repaired."""
    stripped = name.strip()
    if not stripped or len(stripped) > NAME_LIMIT or clean_text(stripped, NAME_LIMIT) != stripped:
        raise _invalid("name", "invalid_text")
    return stripped


def check_url(url: str) -> str:
    """The address, if a browser may safely be sent to it; otherwise a 422 naming the problem."""
    if len(url) > URL_LIMIT or any(unicodedata.category(c) in _FORBIDDEN_CATEGORIES for c in url):
        raise _invalid("url", "invalid_url")
    if not url.startswith("https://"):  # also refuses "HTTPS://", "http://", "javascript:", "data:"
        raise _invalid("url", "https_only")
    if "#" in url:
        raise _invalid("url", "invalid_url")
    try:
        parts = urlsplit(url)
    except ValueError:  # for example a bracket that is not closed
        raise _invalid("url", "invalid_url") from None
    if "@" in parts.netloc or "@" in url:
        raise _invalid("url", "credentials_not_allowed")
    host = parts.hostname
    try:
        parts.port  # noqa: B018  (raises ValueError for a port that is not a number in range)
    except ValueError:
        raise _invalid("url", "invalid_url") from None
    if not host or not host.isascii():
        raise _invalid("url", "invalid_url")
    # Any bracket is an IPv6 (or "IPvFuture") literal: urlsplit drops the brackets, the table and
    # the browser do not.
    if "[" in parts.netloc or "]" in parts.netloc:
        raise _invalid("url", "ip_address_not_allowed")
    bare = host[:-1] if host.endswith(".") else host  # one trailing dot is a legal spelling
    labels = bare.split(".")
    if _is_ip_address(bare) or _NUMBER_LABEL.match(labels[-1]):
        raise _invalid("url", "ip_address_not_allowed")
    if not all(_HOST_LABEL.match(label) for label in labels):  # also refuses an empty label
        raise _invalid("url", "invalid_url")
    # "localhost" and every "*.localhost" are the person's own machine, whatever the DNS says.
    if labels[-1].lower() == "localhost":
        raise _invalid("url", "local_host_not_allowed")
    return url


def _is_unique_violation(error: IntegrityError) -> bool:
    """True for a repeated name (SQLSTATE 23505, the unique index), and for nothing else."""
    code = getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
    return code == "23505" or "ai_tools_name_unique" in str(error.orig)


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


class AiToolService:
    def __init__(self, *, audit: AuditLog) -> None:
        self._audit = audit

    async def list_tools(self, uow: UnitOfWork) -> list[repository.AiToolView]:
        return await repository.list_tools(uow.session, uow.org_id)

    async def _refuse(
        self,
        uow: UnitOfWork,
        actor: Principal,
        action: AuditAction,
        target: str | None,
        ip: str | None,
        reason: str,
        error: ApiError,
    ) -> NoReturn:
        """Refuse, and leave a trace in a transaction of its own (this one is rolled back). The
        request's own transaction ends first: it must not hold a connection while the trace asks
        the pool for another."""
        await uow.release()
        await self._audit.record_independently(
            org_id=uow.org_id,
            action=action,
            outcome=Outcome.FAILURE,
            actor_type=ActorType.USER,
            actor_id=actor.user_id,
            target=target,
            ip=ip,
            details={"reason": reason},
        )
        raise error

    async def create(
        self, uow: UnitOfWork, actor: Principal, body: AiToolIn, *, ip: str | None
    ) -> repository.AiToolView:
        action = AuditAction.AI_TOOL_CREATED
        name = clean_name(body.name)
        url = check_url(body.url)
        await repository.lock_org_tools(uow.session, uow.org_id)
        if await repository.count_tools(uow.session, uow.org_id) >= MAX_ORG_TOOLS:
            await self._refuse(
                uow, actor, action, None, ip, "limit", ApiError(ErrorCode.AI_TOOL_LIMIT, 409)
            )
        created = await repository.insert_tool(
            uow.session, uow.org_id, name=name, url=url, created_by=actor.user_id
        )
        if created is None:
            await self._refuse(
                uow, actor, action, None, ip, "duplicate", ApiError(ErrorCode.AI_TOOL_CONFLICT, 409)
            )
        await self._audit.record(
            uow.session,
            org_id=uow.org_id,
            action=action,
            actor_id=actor.user_id,
            target=f"ai_tool:{created.id}",
            ip=ip,
            details={},
        )
        await uow.commit()
        return created

    async def update(
        self,
        uow: UnitOfWork,
        actor: Principal,
        tool_id: uuid.UUID,
        body: AiToolPatch,
        *,
        ip: str | None,
    ) -> repository.AiToolView:
        action = AuditAction.AI_TOOL_UPDATED
        name = clean_name(body.name) if body.name is not None else None
        url = check_url(body.url) if body.url is not None else None
        target = f"ai_tool:{tool_id}"
        await repository.lock_org_tools(uow.session, uow.org_id)
        try:
            updated = await repository.update_tool(
                uow.session, uow.org_id, tool_id, name=name, url=url
            )
        except IntegrityError as error:
            if not _is_unique_violation(error):
                raise  # anything else is a bug, not a duplicate: let it be a 500 and be seen
            await self._refuse(
                uow,
                actor,
                action,
                target,
                ip,
                "duplicate",
                ApiError(ErrorCode.AI_TOOL_CONFLICT, 409),
            )
        if updated is None:
            raise not_found()
        changed: list[str] = [f for f, v in (("name", name), ("url", url)) if v is not None]
        details: dict[str, Any] = {"fields": ",".join(changed)}
        await self._audit.record(
            uow.session,
            org_id=uow.org_id,
            action=action,
            actor_id=actor.user_id,
            target=target,
            ip=ip,
            details=details,
        )
        await uow.commit()
        return updated

    async def delete(
        self, uow: UnitOfWork, actor: Principal, tool_id: uuid.UUID, *, ip: str | None
    ) -> None:
        await repository.lock_org_tools(uow.session, uow.org_id)
        if not await repository.delete_tool(uow.session, uow.org_id, tool_id):
            raise not_found()
        await self._audit.record(
            uow.session,
            org_id=uow.org_id,
            action=AuditAction.AI_TOOL_DELETED,
            actor_id=actor.user_id,
            target=f"ai_tool:{tool_id}",
            ip=ip,
            details={},
        )
        await uow.commit()
