"""Authentication dependencies: the principal of a request and the role check.

The session layer (M2-03b) supplies a `SessionResolver` that turns the request's cookie into a
user id and organization id. Role and status are never taken from the session: they are read from
the `users` row inside the tenant transaction on every request, so a demotion or a disabled account
applies at once (ADR-0011). Until a resolver is installed nobody is authenticated.
"""

import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from fastapi import Depends, Request
from sqlalchemy import text

from pharos.core.errors import forbidden, not_authenticated
from pharos.core.principal import Principal, Role
from pharos.core.unit_of_work import CurrentOrg, UnitOfWorkDep


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    user_id: uuid.UUID
    org_id: uuid.UUID
    # How the person signed in: "local" (password) or "oidc" (single sign-on).
    method: str = "local"


class SessionResolver(Protocol):
    async def resolve(self, request: Request) -> SessionIdentity | None: ...


class NoSessions:
    async def resolve(self, request: Request) -> SessionIdentity | None:
        return None


async def current_principal(request: Request, uow: UnitOfWorkDep, org_id: CurrentOrg) -> Principal:
    resolver: SessionResolver = request.app.state.sessions
    identity = await resolver.resolve(request)
    # One answer for every failure: whoever probes learns nothing about which part was wrong.
    if identity is None or identity.org_id != org_id:
        raise not_authenticated()
    row = (
        await uow.session.execute(
            text("SELECT role, status FROM users WHERE id = :id"), {"id": identity.user_id}
        )
    ).one_or_none()
    if row is None or row.status != "active":
        raise not_authenticated()
    # Break-glass means administrators only, checked on every request: a person who is no longer an
    # administrator (or whose password sign-in was allowed before the mode was tightened) loses
    # the session at once, not when it expires.
    settings = request.app.state.settings
    if identity.method == "local" and settings.local_login == "break-glass" and row.role != "admin":
        raise not_authenticated()
    return Principal(user_id=identity.user_id, org_id=org_id, role=Role(row.role))


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_role(*allowed: Role) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """Dependency factory: `Depends(require_role(Role.ADMIN))`."""
    roles = frozenset(allowed)

    async def check(principal: CurrentPrincipal) -> Principal:
        if principal.role not in roles:
            raise forbidden()
        return principal

    # Readable by the route-coverage test, which compares it with the role matrix.
    check.allowed_roles = roles  # type: ignore[attr-defined]
    return check
