"""Profile, onboarding and the choices a profile is made from."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from pharos.core.auth import CurrentPrincipal
from pharos.core.unit_of_work import UnitOfWorkDep
from pharos.profile.schemas import (
    DepartmentOut,
    ProfessionOut,
    ProfileIn,
    ProfileOptions,
    ProfileOut,
)
from pharos.profile.service import ProfileService, options

router = APIRouter(tags=["profile"])


def _service(request: Request) -> ProfileService:
    service: ProfileService = request.app.state.profile
    return service


Service = Annotated[ProfileService, Depends(_service)]
AcceptLanguage = Annotated[str | None, Header()]


@router.get("/me/profile")
async def get_profile(
    principal: CurrentPrincipal,
    service: Service,
    uow: UnitOfWorkDep,
    accept_language: AcceptLanguage = None,
) -> ProfileOut:
    return await service.get(uow, principal.user_id, accept_language)


@router.put("/me/profile")
async def put_profile(
    body: ProfileIn,
    principal: CurrentPrincipal,
    service: Service,
    uow: UnitOfWorkDep,
    accept_language: AcceptLanguage = None,
) -> ProfileOut:
    return await service.update(uow, principal.user_id, body, accept_language)


@router.post("/me/onboarding/complete")
async def complete_onboarding(
    principal: CurrentPrincipal,
    service: Service,
    uow: UnitOfWorkDep,
    accept_language: AcceptLanguage = None,
) -> ProfileOut:
    return await service.complete(uow, principal.user_id, accept_language)


@router.get("/me/profile/options")
async def profile_options(principal: CurrentPrincipal) -> ProfileOptions:
    return ProfileOptions(**options())


@router.get("/professions")
async def list_professions(
    principal: CurrentPrincipal, service: Service, uow: UnitOfWorkDep
) -> list[ProfessionOut]:
    rows = await service.professions(uow)
    return [ProfessionOut(id=r.id, code=r.code, names=r.names) for r in rows]


@router.get("/departments")
async def list_departments(
    principal: CurrentPrincipal, service: Service, uow: UnitOfWorkDep
) -> list[DepartmentOut]:
    rows = await service.departments(uow)
    return [DepartmentOut(id=r.id, name=r.name) for r in rows]
