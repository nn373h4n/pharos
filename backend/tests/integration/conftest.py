"""Fixtures for tests that need real PostgreSQL and Redis (`make test-integration`).

A fresh database is created per test session. The two application roles are created if they do
not exist, the baseline migration runs as the owner role, and tests connect as the application role,
exactly as in a deployment.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pharos.core.db import create_engine

OWNER_ROLE, OWNER_PASSWORD = "pharos_owner", "owner-test-only"
APP_ROLE, APP_PASSWORD = "pharos_app", "app-test-only"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@dataclass(frozen=True)
class TestDatabase:
    __test__ = False  # not a test class

    name: str
    owner_url: str
    app_url: str
    alembic: Config


def _require(variable: str) -> str:
    value = os.environ.get(variable)
    if not value:
        pytest.fail(f"{variable} is not set; run `make test-integration`", pytrace=False)
    return value


@pytest.fixture(scope="session")
def redis_url() -> str:
    return _require("PHAROS_TEST_REDIS_URL")


@pytest.fixture(scope="session")
def redis_worker_url() -> str:
    """The URL of the worker's own Redis user (see deploy/redis/start.sh)."""
    return _require("PHAROS_TEST_REDIS_WORKER_URL")


@pytest.fixture(scope="session")
def database() -> Iterator[TestDatabase]:
    admin_url = _require("PHAROS_TEST_ADMIN_URL")
    # The fixture creates login roles with known passwords on the server it talks to.
    if make_url(admin_url).host not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("PHAROS_TEST_ADMIN_URL must point at a local throwaway server", pytrace=False)
    name = f"pharos_test_{uuid.uuid4().hex[:12]}"

    def url_for(role: str, password: str) -> str:
        return (
            make_url(admin_url)
            .set(database=name, username=role, password=password)
            .render_as_string(hide_password=False)
        )

    async def admin(*statements: str) -> None:
        engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                for statement in statements:
                    await connection.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(
        admin(
            f"""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{OWNER_ROLE}') THEN
                CREATE ROLE {OWNER_ROLE} LOGIN PASSWORD '{OWNER_PASSWORD}';
              END IF;
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
                  PASSWORD '{APP_PASSWORD}';
              END IF;
            END $$
            """,
            f"CREATE DATABASE {name} OWNER {OWNER_ROLE}",
            f"GRANT CONNECT ON DATABASE {name} TO {APP_ROLE}",
        )
    )

    owner_url, app_url = url_for(OWNER_ROLE, OWNER_PASSWORD), url_for(APP_ROLE, APP_PASSWORD)
    alembic = Config(str(ALEMBIC_INI))
    patch = pytest.MonkeyPatch()
    patch.setenv("PHAROS_MIGRATIONS_DATABASE_URL", owner_url)
    try:
        command.upgrade(alembic, "head")
        yield TestDatabase(name, owner_url, app_url, alembic)
    finally:
        patch.undo()
        asyncio.run(admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


@pytest.fixture
async def owner_engine(database: TestDatabase) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database.owner_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine(database: TestDatabase) -> AsyncIterator[AsyncEngine]:
    # Built by the production factory, so the pool reset hook is part of what the tests exercise.
    # Tests use it one connection at a time, so the next session reuses the same connection and a
    # leaked setting would show up there.
    engine = create_engine(database.app_url)
    yield engine
    await engine.dispose()


Orgs = tuple[uuid.UUID, uuid.UUID]


@pytest.fixture
async def orgs(owner_engine: AsyncEngine) -> AsyncIterator[Orgs]:
    """Two organizations created by the owner role (the operator task)."""
    ids: list[uuid.UUID] = []
    async with owner_engine.begin() as connection:
        for slug in ("alpha", "beta"):
            row = await connection.execute(
                text("INSERT INTO organizations (name, slug) VALUES (:name, :slug) RETURNING id"),
                {"name": f"Org {slug}", "slug": f"{slug}-{uuid.uuid4().hex[:8]}"},
            )
            ids.append(row.scalar_one())
    yield ids[0], ids[1]
    async with owner_engine.begin() as connection:
        # The audit log keeps its rows when an organization goes away unless they are removed first.
        await connection.execute(
            text("DELETE FROM audit_logs WHERE org_id = ANY(:ids)"), {"ids": ids}
        )
        await connection.execute(
            text("DELETE FROM organizations WHERE id = ANY(:ids)"), {"ids": ids}
        )
