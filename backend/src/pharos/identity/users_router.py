"""People of the organization, for administrators."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from pharos.core.auth import require_role
from pharos.core.client import client_ip
from pharos.core.principal import Principal, Role
from pharos.core.unit_of_work import UnitOfWorkDep
from pharos.identity.admin import UserAdminService
from pharos.identity.schemas import (
    AdminUserOut,
    RoleChange,
    StatusChange,
    UserListOut,
)

router = APIRouter(tags=["users"])


def _service(request: Request) -> UserAdminService:
    service: UserAdminService = request.app.state.user_admin
    return service


Service = Annotated[UserAdminService, Depends(_service)]
Admin = Annotated[Principal, Depends(require_role(Role.ADMIN))]


@router.get("/users")
async def list_users(
    admin: Admin,
    service: Service,
    uow: UnitOfWorkDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> UserListOut:
    page = await service.list_users(uow, limit=limit, offset=offset)
    return UserListOut(
        items=[AdminUserOut.model_validate(u, from_attributes=True) for u in page.items],
        total=page.total,
    )


@router.patch("/users/{user_id}/role")
async def change_role(
    user_id: uuid.UUID,
    body: RoleChange,
    request: Request,
    admin: Admin,
    service: Service,
    uow: UnitOfWorkDep,
) -> AdminUserOut:
    view = await service.change_role(uow, admin, user_id, Role(body.role), ip=client_ip(request))
    return AdminUserOut.model_validate(view, from_attributes=True)


@router.patch("/users/{user_id}/status")
async def change_status(
    user_id: uuid.UUID,
    body: StatusChange,
    request: Request,
    admin: Admin,
    service: Service,
    uow: UnitOfWorkDep,
) -> AdminUserOut:
    view = await service.change_status(uow, admin, user_id, body.status, ip=client_ip(request))
    return AdminUserOut.model_validate(view, from_attributes=True)
