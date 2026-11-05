"""The approved AI tools: everybody who is signed in reads the list, administrators change it."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from pharos.aitools.schemas import AiToolIn, AiToolListOut, AiToolOut, AiToolPatch
from pharos.aitools.service import AiToolService
from pharos.core.auth import require_role
from pharos.core.client import client_ip
from pharos.core.principal import Principal, Role
from pharos.core.unit_of_work import UnitOfWorkDep

router = APIRouter(tags=["ai-tools"])


def _service(request: Request) -> AiToolService:
    service: AiToolService = request.app.state.ai_tools
    return service


Service = Annotated[AiToolService, Depends(_service)]
Anyone = Annotated[Principal, Depends(require_role(Role.ADMIN, Role.EDITOR, Role.MEMBER))]
Admin = Annotated[Principal, Depends(require_role(Role.ADMIN))]


@router.get("/ai-tools")
async def list_ai_tools(who: Anyone, service: Service, uow: UnitOfWorkDep) -> AiToolListOut:
    """The approved tools, by name. Empty until an administrator adds one: a card then offers
    "Copy" only."""
    tools = await service.list_tools(uow)
    return AiToolListOut(items=[AiToolOut.model_validate(t, from_attributes=True) for t in tools])


@router.post("/ai-tools", status_code=201)
async def create_ai_tool(
    body: AiToolIn, request: Request, admin: Admin, service: Service, uow: UnitOfWorkDep
) -> AiToolOut:
    created = await service.create(uow, admin, body, ip=client_ip(request))
    return AiToolOut.model_validate(created, from_attributes=True)


@router.patch("/ai-tools/{tool_id}")
async def update_ai_tool(
    tool_id: uuid.UUID,
    body: AiToolPatch,
    request: Request,
    admin: Admin,
    service: Service,
    uow: UnitOfWorkDep,
) -> AiToolOut:
    updated = await service.update(uow, admin, tool_id, body, ip=client_ip(request))
    return AiToolOut.model_validate(updated, from_attributes=True)


@router.delete("/ai-tools/{tool_id}", status_code=204, response_class=Response)
async def delete_ai_tool(
    tool_id: uuid.UUID, request: Request, admin: Admin, service: Service, uow: UnitOfWorkDep
) -> Response:
    await service.delete(uow, admin, tool_id, ip=client_ip(request))
    return Response(status_code=204)
