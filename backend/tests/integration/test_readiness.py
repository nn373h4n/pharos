import time
from collections.abc import AsyncIterator

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.config import DEFAULT_ALEMBIC_DIR, Settings
from pharos.core.health import Readiness, expected_migration_heads
from pharos.core.heartbeat import HEARTBEAT_KEY, HEARTBEAT_MAX_AGE_SECONDS
from pharos.core.organization import OrganizationResolver
from pharos.main import create_app

from .conftest import TestDatabase

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("organization")]

ALL_OK = {
    "database": "ok",
    "database_role": "ok",
    "redis": "ok",
    "worker": "ok",
    "migrations": "ok",
    "organization": "ok",
}
SLUG = "readiness-test"


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    await client.delete(HEARTBEAT_KEY)
    yield client
    await client.delete(HEARTBEAT_KEY)
    await client.aclose()


@pytest.fixture
async def heartbeat(redis: Redis) -> None:
    await redis.set(HEARTBEAT_KEY, repr(time.time()))


async def drop_organization(owner_engine: AsyncEngine) -> None:
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM organizations WHERE slug = :slug"), {"slug": SLUG}
        )


@pytest.fixture
async def organization(owner_engine: AsyncEngine) -> AsyncIterator[None]:
    await drop_organization(owner_engine)
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO organizations (name, slug) VALUES ('Readiness', :slug)"),
            {"slug": SLUG},
        )
    yield
    await drop_organization(owner_engine)


def readiness(engine: AsyncEngine, redis: Redis) -> Readiness:
    return Readiness(
        engine,
        redis,
        expected_migration_heads(DEFAULT_ALEMBIC_DIR),
        OrganizationResolver(engine, SLUG),
    )


@pytest.mark.anyio
@pytest.mark.usefixtures("heartbeat")
async def test_readiness_is_ok_with_real_services(app_engine: AsyncEngine, redis: Redis) -> None:
    report = await readiness(app_engine, redis).check()
    assert report.model_dump() == {"status": "ok", "checks": ALL_OK}


@pytest.mark.anyio
async def test_readiness_fails_without_a_worker_heartbeat(
    app_engine: AsyncEngine, redis: Redis
) -> None:
    report = await readiness(app_engine, redis).check()
    assert report.status == "unavailable"
    assert report.checks == {**ALL_OK, "worker": "fail"}


@pytest.mark.anyio
async def test_readiness_fails_when_the_worker_heartbeat_is_stale(
    app_engine: AsyncEngine, redis: Redis
) -> None:
    await redis.set(HEARTBEAT_KEY, repr(time.time() - HEARTBEAT_MAX_AGE_SECONDS - 5))
    report = await readiness(app_engine, redis).check()
    assert report.checks == {**ALL_OK, "worker": "fail"}


@pytest.mark.anyio
@pytest.mark.usefixtures("heartbeat")
async def test_readiness_fails_when_the_database_is_on_another_revision(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, redis: Redis
) -> None:
    async with owner_engine.begin() as connection:
        original = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
        await connection.execute(text("UPDATE alembic_version SET version_num = 'bogus'"))
    try:
        report = await readiness(app_engine, redis).check()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :version"), {"version": original}
            )
    assert report.status == "unavailable"
    assert report.checks == {**ALL_OK, "migrations": "fail"}


@pytest.mark.anyio
@pytest.mark.usefixtures("heartbeat")
async def test_readiness_fails_when_the_application_connects_as_the_table_owner(
    owner_engine: AsyncEngine, redis: Redis
) -> None:
    # The classic misconfiguration: both URLs point at the owner role. RLS would silently not apply.
    report = await readiness(owner_engine, redis).check()
    assert report.status == "unavailable"
    assert report.checks["database_role"] == "fail"


@pytest.mark.anyio
@pytest.mark.usefixtures("heartbeat")
async def test_readyz_through_the_real_application(
    database: TestDatabase, redis: Redis, redis_url: str
) -> None:
    settings = Settings(
        environment="test",
        database_url=database.app_url,  # type: ignore[arg-type]
        redis_url=redis_url,  # type: ignore[arg-type]
        org_slug=SLUG,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": ALL_OK}


@pytest.mark.anyio
@pytest.mark.usefixtures("heartbeat")
async def test_readiness_fails_until_the_organization_is_created(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, redis: Redis
) -> None:
    await drop_organization(owner_engine)
    report = await readiness(app_engine, redis).check()
    assert report.status == "unavailable"
    assert report.checks == {**ALL_OK, "organization": "fail"}
