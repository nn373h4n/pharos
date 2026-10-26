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

from pharos.core.config import Settings, get_settings
from pharos.core.db import create_engine
from pharos.core.health import Readiness, expected_migration_heads
from pharos.core.health import router as health_router
from pharos.core.logging import configure_logging, request_id_var

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

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((REQUEST_ID_HEADER, request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url.get_secret_value())
        redis = Redis.from_url(settings.redis_url.get_secret_value())
        app.state.readiness = Readiness(
            engine, redis, expected_migration_heads(settings.alembic_dir)
        )
        logger.info("started", extra={"environment": settings.environment})
        try:
            yield
        finally:
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
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router, prefix=API_PREFIX)
    return app
