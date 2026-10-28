"""One transaction per request, scoped to the organization (ADR-0011).

The service commits explicitly, before the response is built, so a failed commit becomes a 5xx and
never a 200 with lost data. The dependency is declared with `scope="function"`: FastAPI's default
scope runs the exit code after the response has been sent, which is too late to report a failure.
Anything not committed is rolled back when the request ends.

No database connection is taken until the request first uses the session: the organization is set
by a hook that runs whenever a transaction begins (so also after a commit), not when the dependency
is resolved. A request that waits for something slow first (the password hashing queue, an identity
provider) therefore does not hold a pooled connection while it waits, and a burst of such requests
cannot use the pool up. `release()` gives the connection back in the middle of a request.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from pharos.core.errors import ApiError, ErrorCode
from pharos.core.logging import org_id_var
from pharos.core.organization import OrganizationNotConfigured, OrganizationResolver


class UnitOfWork:
    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id

    async def commit(self) -> None:
        await self.session.commit()

    async def release(self) -> None:
        """End the current transaction and return its connection to the pool.

        For a read that is followed by something slow. Nothing may be pending: this rolls back.
        A later statement starts a new transaction, scoped to the organization again.
        """
        await self.session.rollback()


async def current_org(request: Request) -> uuid.UUID:
    resolver: OrganizationResolver = request.app.state.organization
    try:
        return await resolver.resolve()
    except OrganizationNotConfigured:
        raise ApiError(ErrorCode.ORGANIZATION_NOT_CONFIGURED, 503) from None


CurrentOrg = Annotated[uuid.UUID, Depends(current_org)]


async def unit_of_work(request: Request, org_id: CurrentOrg) -> AsyncIterator[UnitOfWork]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:

        @event.listens_for(session.sync_session, "after_begin")
        def _scope_to_organization(_: Session, __: object, connection: Connection) -> None:
            connection.execute(
                text("SELECT set_config('app.org_id', :org_id, true)"), {"org_id": str(org_id)}
            )

        token = org_id_var.set(str(org_id))
        try:
            yield UnitOfWork(session, org_id)
        finally:
            org_id_var.reset(token)
            await session.rollback()


UnitOfWorkDep = Annotated[UnitOfWork, Depends(unit_of_work, scope="function")]
