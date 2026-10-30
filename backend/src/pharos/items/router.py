"""The articles people read, and the review of them by administrators and editors."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from pharos.core.auth import require_role
from pharos.core.client import client_ip
from pharos.core.principal import Principal, Role
from pharos.core.unit_of_work import UnitOfWorkDep
from pharos.items.schemas import ItemListOut, ItemOut, ItemStatus
from pharos.items.service import Action, ItemService

router = APIRouter(tags=["items"])


def _service(request: Request) -> ItemService:
    service: ItemService = request.app.state.items
    return service


Service = Annotated[ItemService, Depends(_service)]
Anyone = Annotated[Principal, Depends(require_role(Role.ADMIN, Role.EDITOR, Role.MEMBER))]
Reviewer = Annotated[Principal, Depends(require_role(Role.ADMIN, Role.EDITOR))]


@router.get("/items")
async def list_items(
    who: Anyone,
    service: Service,
    uow: UnitOfWorkDep,
    status: ItemStatus | None = None,
    source_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> ItemListOut:
    found, total = await service.list_items(
        uow, who, status=status, source=source_id, limit=limit, offset=offset
    )
    return ItemListOut(items=found, total=total)


@router.get("/items/{item_id}")
async def get_item(
    item_id: uuid.UUID, who: Anyone, service: Service, uow: UnitOfWorkDep
) -> ItemOut:
    return await service.get(uow, who, item_id)


async def _act(
    action: Action,
    item_id: uuid.UUID,
    request: Request,
    who: Principal,
    service: ItemService,
    uow: UnitOfWorkDep,
) -> ItemOut:
    return await service.act(uow, who, item_id, action, ip=client_ip(request))


@router.post("/items/{item_id}/hold")
async def hold_item(
    item_id: uuid.UUID, request: Request, who: Reviewer, service: Service, uow: UnitOfWorkDep
) -> ItemOut:
    return await _act("hold", item_id, request, who, service, uow)


@router.post("/items/{item_id}/approve")
async def approve_item(
    item_id: uuid.UUID, request: Request, who: Reviewer, service: Service, uow: UnitOfWorkDep
) -> ItemOut:
    return await _act("approve", item_id, request, who, service, uow)


@router.post("/items/{item_id}/hide")
async def hide_item(
    item_id: uuid.UUID, request: Request, who: Reviewer, service: Service, uow: UnitOfWorkDep
) -> ItemOut:
    return await _act("hide", item_id, request, who, service, uow)


@router.post("/items/{item_id}/restore")
async def restore_item(
    item_id: uuid.UUID, request: Request, who: Reviewer, service: Service, uow: UnitOfWorkDep
) -> ItemOut:
    return await _act("restore", item_id, request, who, service, uow)
