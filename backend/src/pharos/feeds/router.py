"""A person's own feeds: any signed-in person, their own feeds only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from pharos.core.auth import require_role
from pharos.core.client import client_ip
from pharos.core.errors import not_found
from pharos.core.principal import Principal, Role
from pharos.core.unit_of_work import UnitOfWorkDep
from pharos.feeds.schemas import (
    FeedIn,
    FeedItemListOut,
    FeedItemOut,
    FeedListOut,
    FeedOut,
    FeedPatch,
    FeedRunListOut,
    FeedRunOut,
)
from pharos.feeds.service import FeedService

router = APIRouter(tags=["feeds"])


def _service(request: Request) -> FeedService:
    service: FeedService = request.app.state.feeds
    if not service.available:
        # Before the body is read, so a switched-off feature never says that a path exists.
        raise not_found()
    return service


Service = Annotated[FeedService, Depends(_service)]
Person = Annotated[Principal, Depends(require_role(Role.ADMIN, Role.EDITOR, Role.MEMBER))]


@router.get("/me/feeds")
async def list_feeds(
    person: Person,
    service: Service,
    uow: UnitOfWorkDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=1000)] = 0,
) -> FeedListOut:
    items, total = await service.list_feeds(uow, person, limit=limit, offset=offset)
    return FeedListOut(
        items=[FeedOut.model_validate(f, from_attributes=True) for f in items], total=total
    )


@router.post("/me/feeds", status_code=201)
async def create_feed(
    body: FeedIn, request: Request, person: Person, service: Service, uow: UnitOfWorkDep
) -> FeedOut:
    created = await service.create(uow, person, body, ip=client_ip(request))
    return FeedOut.model_validate(created, from_attributes=True)


@router.get("/me/feeds/{feed_id}")
async def get_feed(
    feed_id: uuid.UUID, person: Person, service: Service, uow: UnitOfWorkDep
) -> FeedOut:
    return FeedOut.model_validate(await service.get(uow, person, feed_id), from_attributes=True)


@router.patch("/me/feeds/{feed_id}")
async def update_feed(
    feed_id: uuid.UUID,
    body: FeedPatch,
    request: Request,
    person: Person,
    service: Service,
    uow: UnitOfWorkDep,
) -> FeedOut:
    updated = await service.update(uow, person, feed_id, body, ip=client_ip(request))
    return FeedOut.model_validate(updated, from_attributes=True)


@router.delete("/me/feeds/{feed_id}", status_code=204, response_class=Response)
async def delete_feed(
    feed_id: uuid.UUID, request: Request, person: Person, service: Service, uow: UnitOfWorkDep
) -> Response:
    await service.delete(uow, person, feed_id, ip=client_ip(request))
    return Response(status_code=204)


@router.get("/me/feeds/{feed_id}/runs")
async def list_runs(
    feed_id: uuid.UUID,
    person: Person,
    service: Service,
    uow: UnitOfWorkDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> FeedRunListOut:
    rows = await service.runs(uow, person, feed_id, limit=limit)
    return FeedRunListOut(items=[FeedRunOut.model_validate(r, from_attributes=True) for r in rows])


@router.get("/me/feeds/{feed_id}/items")
async def list_items(
    feed_id: uuid.UUID,
    person: Person,
    service: Service,
    uow: UnitOfWorkDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> FeedItemListOut:
    rows, total = await service.items(uow, person, feed_id, limit=limit, offset=offset)
    return FeedItemListOut(
        items=[FeedItemOut.model_validate(r, from_attributes=True) for r in rows], total=total
    )
