"""A limit on the size of a request body (M5-04, F-25).

Every body the API reads is small JSON, so one limit for all of them is enough. Caddy refuses the
same size first (`request_body` in `deploy/Caddyfile`); this is the second line, for a client that
reaches the API without the proxy and for a body that grows while it is being sent (no
`Content-Length`). A pure ASGI middleware, so a refused request never reaches routing, a session
lookup or a database.

A body that grows past the limit is cut off by raising inside the application's read. A framework
may catch that and answer its own error (FastAPI turns it into a 400), so the middleware remembers
that the limit was passed and then replaces whatever the application answered with the 413.
"""

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pharos.core.errors import ErrorCode, problem_response

MAX_BODY_BYTES = 1024 * 1024  # 1 MiB


class _TooLarge(Exception):
    """The body went over the limit while it was being read."""


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self._max:
            await self._refuse(scope, receive, send)
            return

        seen = 0
        exceeded = False
        started = False  # the application's answer has begun reaching the client

        async def counted() -> Message:
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self._max:
                    exceeded = True
                    raise _TooLarge
            return message

        async def watched(message: Message) -> None:
            nonlocal started
            if exceeded and not started:
                return  # the application's own answer to a cut-off body is dropped
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted, watched)
        except _TooLarge:
            if started:
                raise  # the answer is on its way already; the connection is cut by the server
        if exceeded and not started:
            await self._refuse(scope, receive, send)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = problem_response(
            Request(scope), ErrorCode.PAYLOAD_TOO_LARGE, 413, {"Connection": "close"}
        )
        await response(scope, receive, send)
