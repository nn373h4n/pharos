import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.config import DEFAULT_ALEMBIC_DIR, Settings
from pharos.core.health import Readiness, expected_migration_heads
from pharos.main import create_app

from .conftest import TestDatabase

pytestmark = pytest.mark.integration

ALL_OK = {"database": "ok", "database_role": "ok", "redis": "ok", "migrations": "ok"}


@pytest.mark.anyio
async def test_readiness_is_ok_with_real_services(app_engine: AsyncEngine, redis_url: str) -> None:
    redis = Redis.from_url(redis_url)
    try:
        report = await Readiness(
            app_engine, redis, expected_migration_heads(DEFAULT_ALEMBIC_DIR)
        ).check()
    finally:
        await redis.aclose()
    assert report.model_dump() == {"status": "ok", "checks": ALL_OK}


@pytest.mark.anyio
async def test_readiness_fails_when_the_database_is_on_another_revision(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, redis_url: str
) -> None:
    redis = Redis.from_url(redis_url)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num = 'bogus'"))
        try:
            report = await Readiness(
                app_engine, redis, expected_migration_heads(DEFAULT_ALEMBIC_DIR)
            ).check()
        finally:
            async with owner_engine.begin() as connection:
                await connection.execute(text("UPDATE alembic_version SET version_num = '0001'"))
    finally:
        await redis.aclose()
    assert report.status == "unavailable"
    assert report.checks == {**ALL_OK, "migrations": "fail"}


@pytest.mark.anyio
async def test_readiness_fails_when_the_application_connects_as_the_table_owner(
    owner_engine: AsyncEngine, redis_url: str
) -> None:
    # The classic misconfiguration: both URLs point at the owner role. RLS would silently not apply.
    redis = Redis.from_url(redis_url)
    try:
        report = await Readiness(
            owner_engine, redis, expected_migration_heads(DEFAULT_ALEMBIC_DIR)
        ).check()
    finally:
        await redis.aclose()
    assert report.status == "unavailable"
    assert report.checks["database_role"] == "fail"


@pytest.mark.anyio
async def test_readyz_through_the_real_application(database: TestDatabase, redis_url: str) -> None:
    settings = Settings(
        environment="test",
        database_url=database.app_url,  # type: ignore[arg-type]
        redis_url=redis_url,  # type: ignore[arg-type]
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": ALL_OK}
