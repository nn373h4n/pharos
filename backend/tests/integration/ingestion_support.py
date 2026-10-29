"""Shared fixtures and helpers for the ingestion schema tests (M3-05)."""

import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from pharos.core.db import create_engine

from .conftest import Orgs, TestDatabase

INGESTION_TABLES = ("sources", "fetch_runs", "raw_items", "items", "item_sources")


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Org:
    id: uuid.UUID
    user: uuid.UUID
    other_user: uuid.UUID


@pytest.fixture
async def worker_engine(database: TestDatabase) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database.worker_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def alpha_beta(owner_engine: AsyncEngine, orgs: Orgs) -> tuple[Org, Org]:
    """Two organizations with two users each (created by the owner role, as an operator would)."""

    async def make(org: uuid.UUID, label: str) -> Org:
        async with owner_engine.begin() as connection:
            users = [
                (
                    await connection.execute(
                        text(
                            "INSERT INTO users (org_id, issuer, subject) "
                            "VALUES (:org, 'local', :subject) RETURNING id"
                        ),
                        {"org": org, "subject": f"{label}-{n}"},
                    )
                ).scalar_one()
                for n in (1, 2)
            ]
        return Org(org, users[0], users[1])

    result = (await make(orgs[0], "alpha"), await make(orgs[1], "beta"))
    yield result  # type: ignore[misc]
    async with owner_engine.begin() as connection:
        for org in result:
            for table in ("item_sources", "items", "raw_items", "fetch_runs", "sources"):
                await connection.execute(
                    text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org.id}
                )
            await connection.execute(text("DELETE FROM users WHERE org_id = :o"), {"o": org.id})


@asynccontextmanager
async def session(
    engine: AsyncEngine, org: Org, user: uuid.UUID | None = None
) -> AsyncIterator[AsyncConnection]:
    """A transaction scoped like a request: the organization and, optionally, the person."""
    async with engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org.id)}
        )
        if user is not None:
            await connection.execute(
                text("SELECT set_config('app.user_id', :user, true)"), {"user": str(user)}
            )
        yield connection


async def add_source(
    connection: AsyncConnection,
    org: Org,
    *,
    key: str,
    owner: uuid.UUID | None = None,
    type: str = "rss",
    **columns: object,
) -> uuid.UUID:
    values = {
        "org_id": org.id,
        "owner_user_id": owner,
        "type": type,
        "name": f"Source {key}",
        "config": "{}",
        "dedupe_key": key,
        **columns,
    }
    names = ", ".join(values)
    marks = ", ".join(f":{n}" if n != "config" else "CAST(:config AS jsonb)" for n in values)
    row = await connection.execute(
        text(f"INSERT INTO sources ({names}) VALUES ({marks}) RETURNING id"),
        values,
    )
    return row.scalar_one()


async def add_item(
    connection: AsyncConnection,
    org: Org,
    *,
    url: str,
    owner: uuid.UUID | None = None,
    **columns: object,
) -> uuid.UUID:
    values = {
        "org_id": org.id,
        "owner_user_id": owner,
        "canonical_url": url,
        "url_hash": sha(url),
        "title": "A title",
        "content_sha256": sha("content:" + url),
        **columns,
    }
    names = ", ".join(values)
    marks = ", ".join(f":{n}" for n in values)
    row = await connection.execute(
        text(f"INSERT INTO items ({names}) VALUES ({marks}) RETURNING id"),
        values,
    )
    return row.scalar_one()


async def count(connection: AsyncConnection, table: str) -> int:
    return (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()


@pytest.fixture
def alpha(alpha_beta: tuple[Org, Org]) -> Org:
    return alpha_beta[0]


@pytest.fixture
async def slug(owner_engine: AsyncEngine, alpha: Org) -> str:
    """The slug of the first organization: the worker is told which installation it serves."""
    async with owner_engine.connect() as connection:
        return (
            await connection.execute(
                text("SELECT slug FROM organizations WHERE id = :id"), {"id": alpha.id}
            )
        ).scalar_one()
