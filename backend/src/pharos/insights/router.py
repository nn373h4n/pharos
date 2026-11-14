"""The insight of an article for the person asking."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from pharos.core.auth import require_role
from pharos.core.principal import Principal, Role
from pharos.core.unit_of_work import UnitOfWorkDep
from pharos.insights.schemas import InsightOut
from pharos.insights.service import InsightService

router = APIRouter(tags=["insights"])


def _service(request: Request) -> InsightService:
    service: InsightService = request.app.state.insights
    return service


Service = Annotated[InsightService, Depends(_service)]
Anyone = Annotated[Principal, Depends(require_role(Role.ADMIN, Role.EDITOR, Role.MEMBER))]


@router.get("/items/{item_id}/insight")
async def get_insight(
    item_id: uuid.UUID,
    who: Anyone,
    service: Service,
    uow: UnitOfWorkDep,
    accept_language: Annotated[str | None, Header()] = None,
) -> InsightOut:
    """The stored insight, `pending` while the worker makes it, `absent` if nobody asked, or
    `unavailable`. Never asks the worker."""
    return await service.read(uow, who, item_id, accept_language)


@router.post("/items/{item_id}/insight")
async def ask_insight(
    item_id: uuid.UUID,
    who: Anyone,
    service: Service,
    uow: UnitOfWorkDep,
    accept_language: Annotated[str | None, Header()] = None,
) -> InsightOut:
    """As GET, but asks the worker to make the insight when there is none (CSRF-checked)."""
    return await service.ask(uow, who, item_id, accept_language)
