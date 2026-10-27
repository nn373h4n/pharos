"""Sign-in, sign-out and the current user."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from pharos.core.auth import CurrentPrincipal
from pharos.core.client import client_ip
from pharos.core.csrf import SessionCookie, csrf_token
from pharos.core.errors import not_authenticated
from pharos.core.unit_of_work import UnitOfWorkDep
from pharos.identity.oidc.service import OidcService
from pharos.identity.schemas import (
    AuthConfig,
    LoginRequest,
    LogoutOut,
    OidcConfig,
    SessionOut,
    UserOut,
)
from pharos.identity.service import LocalAuthService

router = APIRouter(tags=["identity"])


def _service(request: Request) -> LocalAuthService:
    service: LocalAuthService = request.app.state.local_auth
    return service


Service = Annotated[LocalAuthService, Depends(_service)]


def _cookie(request: Request) -> SessionCookie:
    cookie: SessionCookie = request.app.state.session_cookie
    return cookie


@router.get("/auth/config")
async def auth_config(request: Request, service: Service) -> AuthConfig:
    oidc: OidcService | None = request.app.state.oidc
    return AuthConfig(
        local_login=service.mode,
        oidc=(
            OidcConfig(provider_name=oidc.provider_name, login_url="/api/auth/oidc/login")
            if oidc
            else None
        ),
    )


@router.post("/auth/local/login")
async def local_login(
    body: LoginRequest,
    request: Request,
    response: Response,
    service: Service,
    uow: UnitOfWorkDep,
) -> SessionOut:
    cookie = _cookie(request)
    result = await service.login(
        uow,
        username=body.username,
        password=body.password,
        ip=client_ip(request),
        previous_session_id=cookie.read(request),
    )
    cookie.set(response, result.session_id)
    token = csrf_token(request.app.state.csrf_key, result.session_id, result.record)
    return SessionOut(
        user=UserOut.model_validate(result.user, from_attributes=True), csrf_token=token
    )


@router.post("/auth/logout", status_code=204, responses={200: {"model": LogoutOut}})
async def logout(request: Request, service: Service, uow: UnitOfWorkDep) -> Response:
    cookie = _cookie(request)
    method = await service.logout(uow, session_id=cookie.read(request), ip=client_ip(request))
    oidc: OidcService | None = request.app.state.oidc
    if method == "oidc" and oidc is not None:
        # The provider has its own session; the web app sends the person there to end it too.
        response: Response = JSONResponse(
            LogoutOut(end_session_url=await oidc.end_session_url()).model_dump()
        )
    else:
        response = Response(status_code=204)
    cookie.clear(response)
    return response


@router.get("/me")
async def me(
    request: Request, principal: CurrentPrincipal, service: Service, uow: UnitOfWorkDep
) -> SessionOut:
    view = await service.user_view(uow, principal.user_id)
    if view is None:
        raise not_authenticated()
    token = csrf_token(request.app.state.csrf_key, request.state.session_id, request.state.session)
    return SessionOut(user=UserOut.model_validate(view, from_attributes=True), csrf_token=token)
