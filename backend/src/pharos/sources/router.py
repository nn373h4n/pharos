"""The organization's sources, for administrators and editors."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from pharos.core.auth import require_role
from pharos.core.client import client_ip
from pharos.core.principal import Principal, Role
from pharos.core.unit_of_work import UnitOfWorkDep
from pharos.sources.schemas import (
    RunListOut,
    RunOut,
    SourceIn,
    SourceListOut,
    SourceOut,
    SourcePatch,
    SourceType,
)
from pharos.sources.service import SourceService

router = APIRouter(tags=["sources"])


def _service(request: Request) -> SourceService:
    service: SourceService = request.app.state.sources
    return service


Service = Annotated[SourceService, Depends(_service)]
Admin = Annotated[Principal, Depends(require_role(Role.ADMIN))]
Reader = Annotated[Principal, Depends(require_role(Role.ADMIN, Role.EDITOR))]


@router.get("/sources")
async def list_sources(
    reader: Reader,
    service: Service,
    uow: UnitOfWorkDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    type: SourceType | None = None,
    enabled: bool | None = None,
) -> SourceListOut:
    items, total = await service.list_sources(
        uow, limit=limit, offset=offset, source_type=type, enabled=enabled
    )
    return SourceListOut(
        items=[SourceOut.model_validate(s, from_attributes=True) for s in items], total=total
    )


@router.post("/sources", status_code=201)
async def create_source(
    body: SourceIn, request: Request, admin: Admin, service: Service, uow: UnitOfWorkDep
) -> SourceOut:
    created = await service.create(uow, admin, body, ip=client_ip(request))
    return SourceOut.model_validate(created, from_attributes=True)


@router.get("/sources/{source_id}")
async def get_source(
    source_id: uuid.UUID, reader: Reader, service: Service, uow: UnitOfWorkDep
) -> SourceOut:
    return SourceOut.model_validate(await service.get(uow, source_id), from_attributes=True)


@router.patch("/sources/{source_id}")
async def update_source(
    source_id: uuid.UUID,
    body: SourcePatch,
    request: Request,
    admin: Admin,
    service: Service,
    uow: UnitOfWorkDep,
) -> SourceOut:
    updated = await service.update(uow, admin, source_id, body, ip=client_ip(request))
    return SourceOut.model_validate(updated, from_attributes=True)


@router.delete("/sources/{source_id}", status_code=204, response_class=Response)
async def delete_source(
    source_id: uuid.UUID, request: Request, admin: Admin, service: Service, uow: UnitOfWorkDep
) -> Response:
    await service.delete(uow, admin, source_id, ip=client_ip(request))
    return Response(status_code=204)


@router.get("/sources/{source_id}/runs")
async def list_runs(
    source_id: uuid.UUID,
    reader: Reader,
    service: Service,
    uow: UnitOfWorkDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> RunListOut:
    rows = await service.runs(uow, source_id, limit=limit)
    return RunListOut(items=[RunOut.model_validate(r, from_attributes=True) for r in rows])
