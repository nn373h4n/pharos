"""Browser-facing single sign-in routes: they redirect, they do not return JSON.

A failed sign-in sends the person back to the sign-in page with a short error code; the page shows a
translated message. The reason is only in the audit log.
"""

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import RedirectResponse, Response

from pharos.core.client import client_ip
from pharos.core.csrf import SessionCookie
from pharos.core.errors import DependencyUnavailable, not_found
from pharos.core.unit_of_work import UnitOfWorkDep
from pharos.identity.oidc.cookies import BindingCookie
from pharos.identity.oidc.service import UNAVAILABLE, OidcFailure, OidcService

router = APIRouter(tags=["identity"])


def _service(request: Request) -> OidcService:
    service: OidcService | None = request.app.state.oidc
    if service is None:
        raise not_found()  # single sign-in is not configured: the routes do not exist
    return service


Service = Annotated[OidcService, Depends(_service)]


def _binding(request: Request) -> BindingCookie:
    cookie: BindingCookie = request.app.state.oidc_binding
    return cookie


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(request: Request, code: str) -> Response:
    response = RedirectResponse("/login?" + urlencode({"error": code}), status_code=303)
    _binding(request).clear(response)
    return _no_store(response)


@router.get("/auth/oidc/login")
async def oidc_login(
    request: Request,
    service: Service,
    next_path: Annotated[str | None, Query(alias="next", max_length=1024)] = None,
) -> Response:
    try:
        started = await service.begin(ip=client_ip(request), next_path=next_path)
    except OidcFailure as failure:
        return _error(request, failure.code)
    except DependencyUnavailable:
        return _error(request, UNAVAILABLE)
    response = RedirectResponse(started.url, status_code=303)
    _binding(request).set(response, started.binding)
    return _no_store(response)


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request, service: Service, uow: UnitOfWorkDep) -> Response:
    params: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        params.setdefault(key, value)  # the first value wins; repeated parameters change nothing
    session_cookie: SessionCookie = request.app.state.session_cookie
    try:
        result = await service.finish(
            uow,
            params=params,
            binding=_binding(request).read(request),
            ip=client_ip(request),
            previous_session_id=session_cookie.read(request),
        )
    except OidcFailure as failure:
        return _error(request, failure.code)
    except DependencyUnavailable:
        return _error(request, UNAVAILABLE)
    response = RedirectResponse(result.next_path, status_code=303)
    session_cookie.set(response, result.session_id)
    _binding(request).clear(response)
    return _no_store(response)
