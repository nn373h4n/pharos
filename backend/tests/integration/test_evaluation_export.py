"""`export-corpus` writes a file that later reaches a cloud model, so its query is a class A gate
of its own (ADR-0015): only shared, current, enriched articles of the organization (M4-09)."""

# ruff: noqa: F811  (fixtures are imported by name)

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from pharos.evaluation.__main__ import collect

from .ingestion_support import Org, add_item, alpha_beta, worker_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def seed(
    connection: AsyncConnection,
    org: Org,
    name: str,
    *,
    fit: bool,
    read: bool = True,
    owner: uuid.UUID | None = None,
    **columns: object,
) -> None:
    """One article; `read` gives it the columns of an article the model has read."""
    values = {**(_now() if read else {}), **columns}
    item = await add_item(connection, org, url=f"https://x.example/{name}", owner=owner, **values)
    if fit:
        await connection.execute(
            text(
                "INSERT INTO item_professions (org_id, item_id, profession_code, relevance) "
                "VALUES (:o, :i, 'accountant', 90)"
            ),
            {"o": org.id, "i": item},
        )


def _now() -> dict[str, object]:
    """The columns of an article the model has read (they must come together)."""
    return {
        "summary": "S",
        "category": "tutorial",
        "quality": 4,
        "enriched_at": datetime.now(UTC),
        "enrichment_version": "v1",
    }


async def exported(engine: AsyncEngine, org: Org, negatives: int) -> set[str]:
    async with engine.begin() as connection:
        found = await collect(connection, org.id, ["accountant"], 50, negatives)
    return {entry.url for entry in found}


async def test_only_shared_current_enriched_articles_of_the_organization_are_exported(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    async with owner_engine.begin() as connection:
        await seed(connection, alpha, "ok-fit", fit=True)
        await seed(connection, alpha, "ok-neg", fit=False)
        for fit in (True, False):
            tag = "fit" if fit else "neg"
            await seed(connection, alpha, f"personal-{tag}", fit=fit, owner=alpha.user)
            await seed(
                connection, alpha, f"held-{tag}", fit=fit, status="held", hold_reason="editor_hold"
            )
            await seed(connection, alpha, f"hidden-{tag}", fit=fit, status="hidden")
            await seed(connection, alpha, f"raw-{tag}", fit=fit, read=False)  # not read yet
            await seed(connection, beta, f"other-org-{tag}", fit=fit)
    assert await exported(worker_engine, alpha, 50) == {
        "https://x.example/ok-fit",
        "https://x.example/ok-neg",
    }
    # As the table owner, which row-level security does not apply to: the query's own filters.
    assert await exported(owner_engine, alpha, 50) == {
        "https://x.example/ok-fit",
        "https://x.example/ok-neg",
    }
