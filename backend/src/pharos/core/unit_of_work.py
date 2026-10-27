"""One transaction per request, scoped to the organization (ADR-0011).

The service commits explicitly, before the response is built, so a failed commit becomes a 5xx and
never a 200 with lost data. The dependency is declared with `scope="function"`: FastAPI's default
scope runs the exit code after the response has been sent, which is too late to report a failure.
Anything not committed is rolled back when the request ends. The organization setting lasts for the
transaction, so a commit ends the scoped part of the request.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pharos.core.errors import ApiError, ErrorCode
from pharos.core.logging import org_id_var
from pharos.core.organization import OrganizationNotConfigured, OrganizationResolver
from pharos.core.rls import set_current_org


class UnitOfWork:
    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id

    async def commit(self) -> None:
        await self.session.commit()


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
        await set_current_org(session, org_id)
        token = org_id_var.set(str(org_id))
        try:
            yield UnitOfWork(session, org_id)
        finally:
            org_id_var.reset(token)
            await session.rollback()


UnitOfWorkDep = Annotated[UnitOfWork, Depends(unit_of_work, scope="function")]
