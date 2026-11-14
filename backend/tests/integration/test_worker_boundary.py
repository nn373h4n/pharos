"""The worker cannot release a held article (F-33, migration 0014, ADR-0017 amendment)."""

import asyncio
import uuid
from typing import Any

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.ingest import repository

from .conftest import TestDatabase
from .ingestion_support import Org, add_item, sha

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def item(owner_engine: AsyncEngine, org: Org, **columns: Any) -> uuid.UUID:
    async with owner_engine.begin() as connection:
        return await add_item(
            connection, org, url=f"https://news.example/{uuid.uuid4().hex}", **columns
        )


async def status_of(owner_engine: AsyncEngine, found: uuid.UUID) -> str | None:
    async with owner_engine.connect() as connection:
        row = await connection.execute(text("SELECT status FROM items WHERE id = :i"), {"i": found})
        value = row.scalar_one_or_none()
        return None if value is None else str(value)


async def as_worker(worker_engine: AsyncEngine, tenant: Org, sql: str, **params: Any) -> int:
    async with worker_engine.begin() as connection:
        await repository.enter_org(connection, tenant.id)
        result = await connection.execute(text(sql), params)
        return result.rowcount  # type: ignore[attr-defined,no-any-return]


@pytest.mark.parametrize("reason", ["link_ip_host", "editor_hold", "low_quality"])
async def test_the_worker_cannot_delete_a_held_article_and_can_delete_a_new_one(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, reason: str
) -> None:
    held = await item(owner_engine, alpha, status="held", hold_reason=reason)
    fresh = await item(owner_engine, alpha)
    assert await as_worker(worker_engine, alpha, "DELETE FROM items WHERE id = :i", i=held) == 0
    assert await status_of(owner_engine, held) == "held"
    assert await as_worker(worker_engine, alpha, "DELETE FROM items WHERE id = :i", i=fresh) == 1
    assert await status_of(owner_engine, fresh) is None  # control: retention still works


INSERT = (
    "INSERT INTO items (org_id, canonical_url, url_hash, title, content_sha256, status, "
    "hold_reason) VALUES (:org, :url, :hash, 'T', :content, :status, :reason)"
)


def row(org: Org, status: str, reason: str | None) -> dict[str, Any]:
    url = f"https://news.example/{uuid.uuid4().hex}"
    return {
        "org": org.id, "url": url, "hash": sha(url), "content": sha("c" + url),
        "status": status, "reason": reason,
    }  # fmt: skip


@pytest.mark.parametrize("reason", ["editor_hold", "low_quality", "made_up"])
async def test_the_worker_inserts_only_new_articles_or_ones_a_rule_holds(
    worker_engine: AsyncEngine, alpha: Org, reason: str
) -> None:
    with pytest.raises(DBAPIError) as refused:
        await as_worker(worker_engine, alpha, INSERT, **row(alpha, "held", reason))
    assert getattr(refused.value.orig, "sqlstate", None) == "42501"  # a row-level policy
    for status, ok in (("new", None), ("held", "link_ip_host"), ("held", "instruction_phrase")):
        assert await as_worker(worker_engine, alpha, INSERT, **row(alpha, status, ok)) == 1


async def test_a_held_article_cannot_be_released_by_deleting_and_inserting_it(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org
) -> None:
    """The attack of F-33, end to end: the delete finds nothing, so the insert of the same link
    as `new` conflicts with the held row, which stays held."""
    url = "https://news.example/held-by-a-person"
    async with owner_engine.begin() as connection:
        held = await add_item(connection, alpha, url=url, status="held", hold_reason="editor_hold")
    assert await as_worker(worker_engine, alpha, "DELETE FROM items WHERE id = :i", i=held) == 0
    with pytest.raises(DBAPIError):
        await as_worker(
            worker_engine, alpha, INSERT,
            org=alpha.id, url=url, hash=sha(url), content=sha("again"), status="new", reason=None,
        )  # fmt: skip
    assert await status_of(owner_engine, held) == "held"


async def test_migration_0014_goes_down_and_up_again(
    database: TestDatabase, owner_engine: AsyncEngine
) -> None:
    async def policies() -> set[str]:
        async with owner_engine.connect() as connection:
            found = await connection.execute(
                text("SELECT policyname FROM pg_policies WHERE tablename = 'items'")
            )
            return {str(r[0]) for r in found}

    mine = {"items_worker_deletes_only_new", "items_worker_inserts_new_or_rule_held"}
    assert mine <= await policies()
    await asyncio.to_thread(command.downgrade, database.alembic, "0013")
    try:
        assert not mine & await policies()
    finally:
        await asyncio.to_thread(command.upgrade, database.alembic, "head")
    assert mine <= await policies()
