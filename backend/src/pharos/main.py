"""FastAPI application factory. Run with `uvicorn --factory pharos.main:create_app`.

There is deliberately no module-level `app`: importing this module must not read the environment
or reconfigure logging.
"""

import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pharos.core.audit import AuditLog
from pharos.core.clock import SystemClock
from pharos.core.config import Settings, get_settings
from pharos.core.csrf import CsrfMiddleware, SessionCookie
from pharos.core.db import create_engine, create_session_factory
from pharos.core.errors import REQUEST_ID_STATE_KEY, install_error_handlers
from pharos.core.health import Readiness, expected_migration_heads
from pharos.core.health import router as health_router
from pharos.core.logging import configure_logging, request_id_var
from pharos.core.organization import OrganizationResolver
from pharos.core.ratelimit import LoginThrottle, RateLimiter
from pharos.core.redis import url_for_database
from pharos.core.secrets import KeyPurpose, derive_key
from pharos.core.sessions import SessionIdentityResolver, SessionStore

logger = logging.getLogger(__name__)

API_PREFIX = "/api"
REQUEST_ID_HEADER = b"x-request-id"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RequestIdMiddleware:
    """Gives every request an id for the logs and echoes it in the response header."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER, b"").decode("latin-1")
        # Anything else (spaces, quotes, control characters, overlong values) is replaced.
        request_id = incoming if _VALID_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        # Error handlers that run outside this middleware (unhandled exceptions) read it from here.
        scope.setdefault("state", {})[REQUEST_ID_STATE_KEY] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                # Error responses from handlers outside this middleware already carry it.
                if not any(name.lower() == REQUEST_ID_HEADER for name, _ in headers):
                    headers.append((REQUEST_ID_HEADER, request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    cookie = SessionCookie(settings.public_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url.get_secret_value())
        redis = Redis.from_url(settings.redis_url.get_secret_value())
        app.state.session_factory = create_session_factory(engine)
        app.state.organization = OrganizationResolver(engine, settings.org_slug)
        # A separate database and short timeouts: sessions fail closed when Redis does not answer.
        session_redis = Redis.from_url(
            url_for_database(settings.redis_url.get_secret_value(), settings.session_redis_db),
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        app.state.session_store = SessionStore(
            session_redis,
            SystemClock(),
            idle_seconds=settings.session_idle_seconds,
            absolute_seconds=settings.session_absolute_seconds,
        )
        app.state.sessions = SessionIdentityResolver(app.state.session_store, cookie.name)
        app.state.login_throttle = LoginThrottle(RateLimiter(session_redis, SystemClock()))
        app.state.audit = AuditLog(
            app.state.session_factory, derive_key(settings.secret_key, KeyPurpose.AUDIT_IP)
        )
        app.state.readiness = Readiness(
            engine,
            redis,
            expected_migration_heads(settings.alembic_dir),
            app.state.organization,
        )
        logger.info("started", extra={"environment": settings.environment})
        try:
            yield
        finally:
            await session_redis.aclose()
            await redis.aclose()
            await engine.dispose()

    show_docs = settings.environment != "production"
    app = FastAPI(
        title="Pharos",
        lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs" if show_docs else None,
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json" if show_docs else None,
    )
    app.state.settings = settings
    install_error_handlers(app)
    app.add_middleware(
        CsrfMiddleware,
        public_url=settings.public_url,
        key=derive_key(settings.secret_key, KeyPurpose.CSRF),
        cookie=cookie,
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router, prefix=API_PREFIX)
    return app
