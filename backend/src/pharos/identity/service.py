"""Local sign-in (ADR-0011). No web framework in here; the router adapts it.

The order of the steps is part of the design:

1. Refuse early when local sign-in is switched off.
2. Throttle by address and account BEFORE any password work, so guessing cannot also exhaust the
   CPU and memory that hashing costs.
3. Refuse over-long passwords without hashing them (a megabyte "password" is an attack).
4. Look the account up and verify a password against its hash, or against the dummy hash when
   there is no such account, so unknown and known accounts cost the same.
5. Every failure, whatever the reason, is one answer (`invalid_credentials`). The real reason goes
   to the audit log only.
6. On success: commit the database first (last login, audit row, a stronger hash if due), then
   create the Redis session, and never reuse a session id from before the sign-in.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from pharos.core.audit import ActorType, AuditAction, AuditLog, Outcome
from pharos.core.errors import ApiError, ErrorCode, not_found
from pharos.core.ratelimit import LoginThrottle
from pharos.core.sessions import SessionRecord, SessionStore
from pharos.core.unit_of_work import UnitOfWork
from pharos.identity import repository
from pharos.identity.passwords import MAX_PASSWORD_LENGTH, PasswordHasher

UNKNOWN_CLIENT = "unknown"


class LocalLoginMode(StrEnum):
    OFF = "off"  # no local sign-in at all (single sign-on only)
    BREAK_GLASS = "break-glass"  # administrators only, for when the identity provider is down
    ON = "on"  # every local account


def _target(account: str) -> str:
    """What the audit log says about the account. A name that cannot be a username is replaced,
    because people sometimes type their password into the username box."""
    return f"local:{account}" if repository.is_valid_username(account) else "local:(invalid)"


def invalid_credentials() -> ApiError:
    return ApiError(ErrorCode.INVALID_CREDENTIALS, 401)


@dataclass(frozen=True, slots=True)
class LoginResult:
    session_id: str
    record: SessionRecord
    user: repository.UserView


class LocalAuthService:
    def __init__(
        self,
        *,
        hasher: PasswordHasher,
        throttle: LoginThrottle,
        audit: AuditLog,
        sessions: SessionStore,
        mode: LocalLoginMode,
    ) -> None:
        self._hasher = hasher
        self._throttle = throttle
        self._audit = audit
        self._sessions = sessions
        self._mode = mode

    @property
    def mode(self) -> LocalLoginMode:
        return self._mode

    async def login(
        self,
        uow: UnitOfWork,
        *,
        username: str,
        password: str,
        ip: str | None,
        previous_session_id: str | None,
    ) -> LoginResult:
        if self._mode is LocalLoginMode.OFF:
            raise not_found()  # looks as if the endpoint did not exist
        account = repository.normalize_username(username)
        client = ip or UNKNOWN_CLIENT
        await self._throttle.check(account, client)

        reason = await self._verify(uow, account, password)
        if isinstance(reason, str):
            await self._refuse(uow, account, client, ip, reason)
            raise invalid_credentials()
        user, rehash = reason

        if rehash is not None:
            await repository.rehash_password(uow.session, uow.org_id, user.id, rehash)
        await repository.touch_last_login(uow.session, uow.org_id, user.id)
        actor = ActorType.LOCAL_ADMIN if user.role == "admin" else ActorType.USER
        await self._audit.record(
            uow.session,
            org_id=uow.org_id,
            action=AuditAction.LOGIN,
            actor_type=actor,
            actor_id=user.id,
            target=_target(account),
            ip=ip,
            details={"method": "local"},
        )
        view = await repository.get_user_view(uow.session, uow.org_id, user.id)
        if view is None:  # cannot happen: the row was read in this very transaction
            raise invalid_credentials()
        await uow.commit()  # the database first, then Redis (ADR-0011)

        session_id, record = await self._sessions.create(user.id, uow.org_id)
        if previous_session_id:
            await self._sessions.delete(previous_session_id)  # no session survives a sign-in
        await self._throttle.succeeded(account, client)
        return LoginResult(session_id, record, view)

    async def _verify(
        self, uow: UnitOfWork, account: str, password: str
    ) -> str | tuple[repository.LocalUser, str | None]:
        """Returns the user (and a stronger hash to store) or the audit reason for refusing."""
        user = await repository.find_local_user(uow.session, uow.org_id, account)
        # Hashing may wait in a queue for seconds; the connection goes back to the pool meanwhile,
        # so a burst of sign-ins cannot keep the whole API waiting for a free connection.
        await uow.release()
        if len(password) > MAX_PASSWORD_LENGTH:
            return "password_too_long"
        outcome = await self._hasher.verify(user.password_hash if user else None, password)
        if user is None or user.password_hash is None:
            return "unknown_user"
        if not outcome.ok:
            return "bad_password"
        if user.status != "active":
            return "inactive_user"
        if self._mode is LocalLoginMode.BREAK_GLASS and user.role != "admin":
            return "not_allowed"
        rehash = await self._hasher.hash(password) if outcome.needs_rehash else None
        return user, rehash

    async def _refuse(
        self, uow: UnitOfWork, account: str, client: str, ip: str | None, reason: str
    ) -> None:
        await self._audit.record_independently(
            org_id=uow.org_id,
            action=AuditAction.LOGIN,
            outcome=Outcome.FAILURE,
            actor_type=ActorType.SYSTEM,
            target=_target(account),
            ip=ip,
            details={"method": "local", "reason": reason},
        )

    async def user_view(self, uow: UnitOfWork, user_id: uuid.UUID) -> repository.UserView | None:
        return await repository.get_user_view(uow.session, uow.org_id, user_id)

    async def logout(
        self, uow: UnitOfWork, *, session_id: str | None, ip: str | None
    ) -> str | None:
        """End the caller's own session. Idempotent: no session, nothing to do.

        Returns how that session was created ("local" or "oidc"), or None if there was none.
        """
        if not session_id:
            return None
        record = await self._sessions.get(session_id)
        await self._sessions.delete(session_id)
        if record is not None and record.org_id == uow.org_id:
            await self._audit.record(
                uow.session,
                org_id=uow.org_id,
                action=AuditAction.LOGOUT,
                actor_id=record.user_id,
                ip=ip,
            )
            await uow.commit()
        return record.method if record is not None else None
