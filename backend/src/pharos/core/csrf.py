"""CSRF protection and the session cookie (ADR-0011).

Two independent checks guard every request that changes state (POST, PUT, PATCH, DELETE):

1. The `Origin` header must equal `PHAROS_PUBLIC_URL` exactly. A missing header or the string "null"
   is refused. The `Host` header is never used: the proxy rewrites it. This covers sign-in too,
   where no session exists yet (login CSRF).
2. When the request carries a valid session, the `X-CSRF-Token` header must equal the HMAC of that
   session, computed with a key derived from `PHAROS_SECRET_KEY`. A token is bound to one session id
   and one seed, so it stops working when the session is rotated.

The cookie is `__Host-pharos_session` (Secure, HttpOnly, SameSite=Lax, Path=/, no Domain) whenever
the public address is https. Plain-http development (localhost) cannot use the `__Host-` prefix,
which browsers only accept together with Secure, so there the name is `pharos_session`.
"""

import base64
import hashlib
import hmac
import logging
from typing import Any

from fastapi import Request
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from pharos.core.errors import DependencyUnavailable, ErrorCode, problem_response
from pharos.core.sessions import SessionRecord

logger = logging.getLogger(__name__)

CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SESSION_COOKIE = "pharos_session"
HOST_PREFIXED_SESSION_COOKIE = f"__Host-{SESSION_COOKIE}"


def csrf_token(key: bytes, session_id: str, record: SessionRecord) -> str:
    digest = hmac.new(key, f"{session_id}.{record.csrf_seed}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def token_matches(key: bytes, session_id: str, record: SessionRecord, supplied: str | None) -> bool:
    if not supplied:
        return False
    expected = csrf_token(key, session_id, record)
    return hmac.compare_digest(expected.encode(), supplied.encode())


def origin_allowed(origin: str | None, public_url: str) -> bool:
    return origin is not None and origin != "null" and origin == public_url


class SessionCookie:
    def __init__(self, public_url: str) -> None:
        self.secure = public_url.startswith("https://")
        self.name = HOST_PREFIXED_SESSION_COOKIE if self.secure else SESSION_COOKIE

    def read(self, request: Request) -> str | None:
        return request.cookies.get(self.name)

    def set(self, response: Response, session_id: str) -> None:
        # No max_age: a browser session cookie. The server enforces the idle and absolute limits.
        response.set_cookie(
            self.name,
            session_id,
            secure=self.secure,
            httponly=True,
            samesite="lax",
            path="/",
        )

    def clear(self, response: Response) -> None:
        response.delete_cookie(
            self.name, secure=self.secure, httponly=True, samesite="lax", path="/"
        )


class CsrfMiddleware:
    """Pure ASGI middleware, so a refused request never reaches routing or a database."""

    def __init__(self, app: ASGIApp, *, public_url: str, key: bytes, cookie: SessionCookie) -> None:
        self.app = app
        self._public_url = public_url
        self._key = key
        self._cookie = cookie

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # A WebSocket handshake is a GET that carries cookies, so the browser's same-origin
            # protections do not apply to it. There are no WebSocket routes today; this keeps a
            # future one from being reachable cross-site.
            origin = Headers(scope=scope).get("origin")
            if not origin_allowed(origin, self._public_url):
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if not origin_allowed(request.headers.get("origin"), self._public_url):
            await self._refuse(request, scope, receive, send)
            return
        session_id = self._cookie.read(request)
        if session_id:
            store: Any = request.app.state.session_store
            try:
                record = await store.get(session_id)
            except DependencyUnavailable:
                logger.exception("csrf: session store unavailable")
                response = problem_response(request, ErrorCode.SERVICE_UNAVAILABLE, 503)
                await response(scope, receive, send)
                return
            # A cookie that names no live session is an anonymous request: only the Origin check
            # applies, and endpoints that need a user answer 401 on their own.
            if record is not None and not token_matches(
                self._key, session_id, record, request.headers.get(CSRF_HEADER)
            ):
                await self._refuse(request, scope, receive, send)
                return
        await self.app(scope, receive, send)

    async def _refuse(self, request: Request, scope: Scope, receive: Receive, send: Send) -> None:
        response = problem_response(request, ErrorCode.CSRF_FAILED, 403)
        await response(scope, receive, send)
