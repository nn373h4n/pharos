"""Administrators managing the people of their organization: list, change role, change status.

Rules (ADR-0011):

- Only administrators call this. Role and status come from the database on every request, and the
  caller is checked again here, after the locks below, against what is committed right now.
- Nobody changes their own role or status, and the caller must still be an active administrator
  once the administrator rows are locked (in a fixed order). So an administrator who is changed
  is never the only one left: the caller is another one, and two administrators changing each
  other at the same moment queue up, the second finding the first one's change and being refused.
  The organization always keeps at least one administrator; no separate "last administrator" rule
  is needed.
- A refusal by the service (changing oneself, a missing target, a failed rule) leaves a failure row
  in the audit log (written on its own, because this transaction is rolled back). A call by someone
  who is not an administrator is refused earlier, by the role check, and leaves only the request
  log: it never reaches this code.
- A change ends the target's sessions after the commit. The next request would already see the new
  role or status, so failing to reach Redis is logged, not turned into an error: the database is
  the authority.
- The answer is read before the commit, so it describes the state that was committed.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import NoReturn

from pharos.core.audit import ActorType, AuditAction, AuditLog, Outcome
from pharos.core.errors import ApiError, ErrorCode, forbidden, not_found
from pharos.core.principal import Principal, Role
from pharos.core.sessions import SessionStore, SessionStoreUnavailable
from pharos.core.unit_of_work import UnitOfWork
from pharos.identity import repository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UserPage:
    items: list[repository.AdminUserView]
    total: int


class UserAdminService:
    def __init__(self, *, audit: AuditLog, sessions: SessionStore) -> None:
        self._audit = audit
        self._sessions = sessions

    async def list_users(self, uow: UnitOfWork, *, limit: int, offset: int) -> UserPage:
        items, total = await repository.list_users(
            uow.session, uow.org_id, limit=limit, offset=offset
        )
        return UserPage(items, total)

    async def _refuse(
        self,
        uow: UnitOfWork,
        actor: Principal,
        target_id: uuid.UUID,
        action: AuditAction,
        ip: str | None,
        reason: str,
        error: ApiError,
    ) -> NoReturn:
        """Refuse, and leave a trace in a transaction of its own (this one is rolled back)."""
        await self._audit.record_independently(
            org_id=uow.org_id,
            action=action,
            outcome=Outcome.FAILURE,
            actor_type=ActorType.USER,
            actor_id=actor.user_id,
            target=f"user:{target_id}",
            ip=ip,
            details={"reason": reason},
        )
        raise error

    async def _begin(
        self,
        uow: UnitOfWork,
        actor: Principal,
        target_id: uuid.UUID,
        action: AuditAction,
        ip: str | None,
    ) -> repository.UserState:
        if target_id == actor.user_id:
            await self._refuse(
                uow,
                actor,
                target_id,
                action,
                ip,
                "own_account",
                ApiError(ErrorCode.OWN_ACCOUNT, 409),
            )
        admins = await repository.lock_active_admins(uow.session, uow.org_id)
        if actor.user_id not in admins:
            # Demoted or disabled by someone else after this request was authenticated.
            await self._refuse(uow, actor, target_id, action, ip, "not_admin", forbidden())
        target = await repository.lock_user(uow.session, uow.org_id, target_id)
        if target is None:
            await self._refuse(uow, actor, target_id, action, ip, "not_found", not_found())
        return target

    async def change_role(
        self,
        uow: UnitOfWork,
        actor: Principal,
        target_id: uuid.UUID,
        role: Role,
        *,
        ip: str | None,
    ) -> repository.AdminUserView:
        action = AuditAction.USER_ROLE_CHANGED
        target = await self._begin(uow, actor, target_id, action, ip)
        if target.status == "deprovisioned":
            # The identity provider decides about this person; do not hand out rights they could
            # get back if the provider restores the account.
            await self._refuse(
                uow,
                actor,
                target_id,
                action,
                ip,
                "status_locked",
                ApiError(ErrorCode.STATUS_LOCKED, 409),
            )
        changed = target.role != role.value
        if changed:
            await repository.set_role(uow.session, uow.org_id, target.id, role.value)
            await self._audit.record(
                uow.session,
                org_id=uow.org_id,
                action=action,
                actor_id=actor.user_id,
                target=f"user:{target.id}",
                ip=ip,
                details={"from": target.role, "to": role.value},
            )
        view = await self._view(uow, target.id)
        if changed:
            await uow.commit()
            await self._end_sessions(uow.org_id, actor.user_id, target.id, ip)
        return view

    async def change_status(
        self,
        uow: UnitOfWork,
        actor: Principal,
        target_id: uuid.UUID,
        status: str,
        *,
        ip: str | None,
    ) -> repository.AdminUserView:
        action = AuditAction.USER_STATUS_CHANGED
        target = await self._begin(uow, actor, target_id, action, ip)
        if target.status == "deprovisioned":  # the identity provider decides
            await self._refuse(
                uow,
                actor,
                target_id,
                action,
                ip,
                "status_locked",
                ApiError(ErrorCode.STATUS_LOCKED, 409),
            )
        changed = target.status != status
        if changed:
            await repository.set_status(uow.session, uow.org_id, target.id, status)
            await self._audit.record(
                uow.session,
                org_id=uow.org_id,
                action=action,
                actor_id=actor.user_id,
                target=f"user:{target.id}",
                ip=ip,
                details={"from": target.status, "to": status},
            )
        view = await self._view(uow, target.id)
        if changed:
            await uow.commit()
            await self._end_sessions(uow.org_id, actor.user_id, target.id, ip)
        return view

    async def _view(self, uow: UnitOfWork, user_id: uuid.UUID) -> repository.AdminUserView:
        view = await repository.get_admin_view(uow.session, uow.org_id, user_id)
        if view is None:  # cannot happen: the row is locked in this transaction
            raise not_found()
        return view

    async def _end_sessions(
        self, org_id: uuid.UUID, actor_id: uuid.UUID, user_id: uuid.UUID, ip: str | None
    ) -> None:
        try:
            removed = await self._sessions.revoke_user(user_id)
        except SessionStoreUnavailable:
            logger.warning("could not end the sessions of a changed user (store unavailable)")
            await self._audit.record_independently(
                org_id=org_id,
                action=AuditAction.SESSIONS_REVOKED,
                outcome=Outcome.FAILURE,
                actor_type=ActorType.USER,
                actor_id=actor_id,
                target=f"user:{user_id}",
                ip=ip,
                details={"reason": "store_unavailable"},
            )
            return
        await self._audit.record_independently(
            org_id=org_id,
            action=AuditAction.SESSIONS_REVOKED,
            outcome=Outcome.SUCCESS,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            target=f"user:{user_id}",
            ip=ip,
            details={"removed": removed},
        )
