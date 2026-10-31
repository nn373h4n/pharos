"""The administrator's view of the model layer."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from pharos.core.auth import require_role
from pharos.core.client import client_ip
from pharos.core.principal import Principal, Role
from pharos.core.unit_of_work import UnitOfWorkDep
from pharos.llmadmin.schemas import KeyTestOut, KeyTestStartedOut, StatusOut
from pharos.llmadmin.service import ModelAdminService

router = APIRouter(tags=["llm"])


def _service(request: Request) -> ModelAdminService:
    service: ModelAdminService = request.app.state.llm_admin
    return service


Service = Annotated[ModelAdminService, Depends(_service)]
Admin = Annotated[Principal, Depends(require_role(Role.ADMIN))]


@router.get("/llm/status")
async def status(admin: Admin, service: Service, uow: UnitOfWorkDep) -> StatusOut:
    return await service.status(uow)


@router.post("/llm/key-test", status_code=202)
async def start_key_test(
    request: Request, admin: Admin, service: Service, uow: UnitOfWorkDep
) -> KeyTestStartedOut:
    """Ask the worker to check the key with one tiny call. Answers at once; read the answer with
    the test id."""
    return await service.start_key_test(uow, admin, ip=client_ip(request))


@router.get("/llm/key-test/{test_id}")
async def read_key_test(
    test_id: uuid.UUID, admin: Admin, service: Service, uow: UnitOfWorkDep
) -> KeyTestOut:
    return await service.key_test_result(uow, test_id)
