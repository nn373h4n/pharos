"""One error format for the whole API: RFC 9457 `application/problem+json`.

The `code` is stable and documented in docs/API.md; the browser maps it to a translated message, so
no user-facing text is produced here. Details never include request data (it may hold secrets) or
internal exception text.
"""

import logging
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"
REQUEST_ID_STATE_KEY = "request_id"


class ErrorCode(StrEnum):
    NOT_AUTHENTICATED = "not_authenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    VALIDATION_FAILED = "validation_failed"
    CSRF_FAILED = "csrf_failed"
    ORGANIZATION_NOT_CONFIGURED = "organization_not_configured"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"
    HTTP_ERROR = "http_error"


_TITLES = {
    ErrorCode.NOT_AUTHENTICATED: "Not authenticated",
    ErrorCode.FORBIDDEN: "Forbidden",
    ErrorCode.NOT_FOUND: "Not found",
    ErrorCode.METHOD_NOT_ALLOWED: "Method not allowed",
    ErrorCode.VALIDATION_FAILED: "The request is not valid",
    ErrorCode.CSRF_FAILED: "The request was refused",
    ErrorCode.ORGANIZATION_NOT_CONFIGURED: "The organization is not set up yet",
    ErrorCode.SERVICE_UNAVAILABLE: "Service unavailable",
    ErrorCode.INTERNAL_ERROR: "Internal error",
    ErrorCode.HTTP_ERROR: "Request failed",
}


class ApiError(Exception):
    """Raised by services and dependencies; rendered as a problem document."""

    def __init__(
        self,
        code: ErrorCode,
        status: int,
        *,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.status = status
        self.headers = headers or {}
        self.extra = extra


class DependencyUnavailable(Exception):
    """A required service (Redis, ...) did not answer. The request fails closed with a 503."""


def not_authenticated() -> ApiError:
    return ApiError(ErrorCode.NOT_AUTHENTICATED, 401)


def forbidden() -> ApiError:
    return ApiError(ErrorCode.FORBIDDEN, 403)


def not_found() -> ApiError:
    """Also used for objects of another organization: their existence is not revealed."""
    return ApiError(ErrorCode.NOT_FOUND, 404)


def problem_response(
    request: Request,
    code: ErrorCode,
    status: int,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    request_id = getattr(request.state, REQUEST_ID_STATE_KEY, None)
    body: dict[str, Any] = {
        "type": f"urn:pharos:problem:{code.value}",
        "title": _TITLES[code],
        "status": status,
        "code": code.value,
        **extra,
    }
    response_headers = dict(headers or {})
    if request_id:
        body["request_id"] = request_id
        response_headers["X-Request-ID"] = request_id
    return JSONResponse(body, status_code=status, headers=response_headers, media_type=PROBLEM_JSON)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return problem_response(request, exc.code, exc.status, exc.headers, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Only where and what kind of mistake: the message and input could echo a password.
        errors = [
            {"loc": [str(part) for part in error["loc"]], "type": error["type"]}
            for error in exc.errors()
        ]
        return problem_response(request, ErrorCode.VALIDATION_FAILED, 422, errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            404: ErrorCode.NOT_FOUND,
            405: ErrorCode.METHOD_NOT_ALLOWED,
            401: ErrorCode.NOT_AUTHENTICATED,
            403: ErrorCode.FORBIDDEN,
        }.get(exc.status_code, ErrorCode.HTTP_ERROR)
        headers = {k: v for k, v in (exc.headers or {}).items() if k.lower() == "allow"}
        return problem_response(request, code, exc.status_code, headers)

    @app.exception_handler(DependencyUnavailable)
    async def _unavailable(request: Request, exc: DependencyUnavailable) -> JSONResponse:
        logger.error("dependency unavailable", exc_info=(type(exc), exc, exc.__traceback__))
        return problem_response(request, ErrorCode.SERVICE_UNAVAILABLE, 503)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled exception",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"path": request.url.path},
        )
        return problem_response(request, ErrorCode.INTERNAL_ERROR, 500)
