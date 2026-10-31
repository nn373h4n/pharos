"""FastAPI application factory. Run with `uvicorn --factory pharos.main:create_app`.

There is deliberately no module-level `app`: importing this module must not read the environment
or reconfigure logging.
"""

import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

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
from pharos.core.queue import CeleryQueue
from pharos.core.ratelimit import LoginThrottle, RateLimiter
from pharos.core.redis import url_for_database
from pharos.core.secrets import KeyPurpose, derive_key
from pharos.core.sessions import SessionIdentityResolver, SessionStore
from pharos.feeds.router import router as feeds_router
from pharos.feeds.service import FeedService
from pharos.identity.admin import UserAdminService
from pharos.identity.oidc.client import IdpClient
from pharos.identity.oidc.cookies import BindingCookie
from pharos.identity.oidc.router import router as oidc_router
from pharos.identity.oidc.service import OidcService
from pharos.identity.passwords import PasswordHasher
from pharos.identity.router import router as identity_router
from pharos.identity.service import LocalAuthService, LocalLoginMode
from pharos.identity.users_router import router as users_router
from pharos.items.router import router as items_router
from pharos.items.service import ItemService
from pharos.llmadmin.router import router as llm_admin_router
from pharos.llmadmin.service import ModelAdminService, ModelSettings
from pharos.profile.router import router as profile_router
from pharos.profile.service import ProfileService
from pharos.sources.router import router as sources_router
from pharos.sources.service import SourceService

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
        app.state.sessions = SessionIdentityResolver(
            app.state.session_store, cookie.name, allow_local=settings.local_login != "off"
        )
        limiter = RateLimiter(session_redis, SystemClock())
        app.state.login_throttle = LoginThrottle(limiter)
        app.state.audit = AuditLog(
            app.state.session_factory, derive_key(settings.secret_key, KeyPurpose.AUDIT_IP)
        )
        app.state.profile = ProfileService(settings.default_locale)
        app.state.oidc = None
        if settings.oidc_issuer and settings.oidc_client_id:
            secret = settings.oidc_client_secret
            app.state.oidc = OidcService(
                idp=IdpClient(
                    None,
                    SystemClock(),
                    issuer=settings.oidc_issuer,
                    client_id=settings.oidc_client_id,
                    client_secret=secret.get_secret_value() if secret else None,
                    allow_http=settings.environment != "production",
                ),
                redis=session_redis,
                clock=SystemClock(),
                audit=app.state.audit,
                sessions=app.state.session_store,
                limiter=app.state.login_throttle._limiter,
                public_url=settings.public_url,
                scopes=settings.oidc_scopes,
                provider_name=settings.oidc_provider_name,
                department_claim=settings.oidc_department_claim,
                entra_tenant=settings.entra_tenant_id,
                entra_allow_guests=settings.entra_allow_guests,
            )
        # The host of this installation is never a source, whatever name it is written with.
        own_host = (urlsplit(settings.public_url).hostname or "").lower()
        queue = CeleryQueue(settings.redis_url.get_secret_value())
        app.state.sources = SourceService(
            audit=app.state.audit,
            blocked_hosts=[own_host] if own_host else [],
            redis=redis,  # the broker's database: the trial fetch keeps its markers there
            queue=queue,
        )
        app.state.llm_admin = ModelAdminService(
            audit=app.state.audit,
            clock=SystemClock(),
            settings=ModelSettings(
                provider=settings.llm_provider,
                model_filter=settings.llm_model_filter,
                model_enrichment=settings.llm_model_enrichment,
                model_insight=settings.llm_model_insight,
                daily_cap_micro_usd=settings.llm_daily_budget_micro_usd,
            ),
            redis=redis,
            queue=queue,
        )
        app.state.items = ItemService(audit=app.state.audit)
        app.state.feeds = FeedService(
            audit=app.state.audit,
            blocked_hosts=[own_host] if own_host else [],
            available=settings.personal_feeds,
            limiter=limiter,
        )
        app.state.user_admin = UserAdminService(
            audit=app.state.audit, sessions=app.state.session_store
        )
        app.state.local_auth = LocalAuthService(
            hasher=PasswordHasher(
                memory_kib=settings.password_hash_memory_kib,
                time_cost=settings.password_hash_time_cost,
            ),
            throttle=app.state.login_throttle,
            audit=app.state.audit,
            sessions=app.state.session_store,
            mode=LocalLoginMode(settings.local_login),
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
            if app.state.oidc is not None:
                await app.state.oidc.idp.aclose()
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
    app.state.session_cookie = cookie
    app.state.oidc_binding = BindingCookie(settings.public_url)
    app.state.oidc = None
    app.state.csrf_key = derive_key(settings.secret_key, KeyPurpose.CSRF)
    install_error_handlers(app)
    app.add_middleware(
        CsrfMiddleware,
        public_url=settings.public_url,
        key=app.state.csrf_key,
        cookie=cookie,
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router, prefix=API_PREFIX)
    app.include_router(identity_router, prefix=API_PREFIX)
    app.include_router(profile_router, prefix=API_PREFIX)
    app.include_router(users_router, prefix=API_PREFIX)
    app.include_router(sources_router, prefix=API_PREFIX)
    app.include_router(items_router, prefix=API_PREFIX)
    app.include_router(feeds_router, prefix=API_PREFIX)
    app.include_router(llm_admin_router, prefix=API_PREFIX)
    app.include_router(oidc_router, prefix=API_PREFIX)
    return app
