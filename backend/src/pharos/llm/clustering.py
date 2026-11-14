"""Embeddings of public articles and clusters of near-duplicates (M4-05, ADR-0007, ADR-0021).

A sweep, run by the worker every minute, takes the public articles the model has read and that have
no vector for the configured model, embeds each through the gateway (class A only, masked, booked
and recorded like any model call), stores the vector, and looks for the nearest article of the last
days. If it is at least as similar as the threshold, both get the same `cluster_id`. Articles are
never merged or deleted: a wrong match only hides one article behind another in the Home feed, and
the cluster can be undone by clearing the column.

Only the worker runs this, as the worker role inside row-level security. Placing an article takes a
lock per organization, so two sweeps cannot cluster the same neighbours at once; the (slow) call to
the embedder is made before the lock is taken.
"""

import logging
import math
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from pharos.llm.embedding import Embedded
from pharos.llm.errors import (
    BudgetExhausted,
    BudgetUnavailable,
    CallRateExceeded,
    LLMDisabled,
    LLMNotConfigured,
    NotPublicArticle,
    ProviderFailed,
    ProviderRejected,
    StagePaused,
)
from pharos.llm.repository import load_public_article
from pharos.llm.types import PublicArticleText

logger = logging.getLogger(__name__)

MAX_FAILURES_IN_A_ROW = 3
# A story has at most this many copies in one cluster. Past it, a new copy stands alone: a limit on
# how many articles can be hidden behind one, whatever the vectors say.
MAX_CLUSTER_SIZE = 20

# The reasons a sweep stops early. None of them is the article's fault.
_STOPS = (
    BudgetExhausted,
    BudgetUnavailable,
    CallRateExceeded,
    LLMDisabled,
    LLMNotConfigured,
    ProviderFailed,
    StagePaused,
)


class Embedder(Protocol):
    async def embed(self, *, org_id: uuid.UUID, article: PublicArticleText) -> Embedded: ...


@dataclass(slots=True)
class SweepResult:
    embedded: int = 0
    clustered: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: str | None = None


def vector_literal(vector: tuple[float, ...]) -> str:
    """The text form pgvector reads. Numbers only: `float` is checked before this is built."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


async def _enter(connection: AsyncConnection, org: uuid.UUID) -> None:
    await connection.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org)})


async def pending_items(
    connection: AsyncConnection, org: uuid.UUID, model: str, window_days: int, limit: int
) -> list[uuid.UUID]:
    """Public, current articles the model has read and that have no vector for `model` yet, oldest
    first. Older ones are left alone: they are outside the window and cannot be compared."""
    await _enter(connection, org)
    rows = await connection.execute(
        text(
            "SELECT i.id FROM items i WHERE i.org_id = :org AND i.owner_user_id IS NULL "
            "AND i.status = 'new' AND i.enriched_at IS NOT NULL AND i.summary IS NOT NULL "
            "AND i.created_at >= now() - make_interval(days => :days) "
            "AND NOT EXISTS (SELECT 1 FROM item_embeddings e WHERE e.org_id = i.org_id "
            "AND e.item_id = i.id AND e.model = :model) "
            "ORDER BY i.created_at, i.id LIMIT :n"
        ),
        {"org": org, "model": model, "days": window_days, "n": limit},
    )
    return [row.id for row in rows]


async def place(
    connection: AsyncConnection,
    org: uuid.UUID,
    item: uuid.UUID,
    embedded: Embedded,
    *,
    threshold: float,
    window_days: int,
) -> bool | None:
    """Store the vector of `item` and join it to the cluster of its nearest neighbour, if that is
    similar enough. Returns True if it joined or made a cluster, False if it stands alone, and None
    if the article is no longer a public, current one (nothing is stored then)."""
    await _enter(connection, org)
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
        {"name": f"pharos.embed:{org}"},
    )
    mine = (
        await connection.execute(
            text(
                "SELECT cluster_id FROM items WHERE org_id = :org AND id = :id "
                "AND owner_user_id IS NULL AND status = 'new' FOR UPDATE"
            ),
            {"org": org, "id": item},
        )
    ).first()
    if mine is None:
        return None
    vector = vector_literal(embedded.vector)
    await connection.execute(
        text(
            "INSERT INTO item_embeddings (org_id, item_id, model, dim, embedding) "
            "VALUES (:org, :id, :model, :dim, CAST(:vec AS vector)) "
            "ON CONFLICT (org_id, item_id, model) DO NOTHING"
        ),
        {"org": org, "id": item, "model": embedded.model, "dim": embedded.dim, "vec": vector},
    )
    # Only vectors of the same model and size are compared, and only with articles that a person
    # could be shown: public, current, read by the model, recent. A stored vector with no length
    # (it cannot be stored, but this does not rely on that) is not a neighbour.
    near = (
        await connection.execute(
            text(
                "SELECT e.item_id, i.cluster_id, 1 - (e.embedding <=> CAST(:vec AS vector)) AS sim "
                "FROM item_embeddings e JOIN items i ON i.org_id = e.org_id AND i.id = e.item_id "
                "WHERE e.org_id = :org AND e.model = :model AND e.dim = :dim AND e.item_id <> :id "
                "AND vector_norm(e.embedding) > 0 "
                "AND i.owner_user_id IS NULL AND i.status = 'new' AND i.summary IS NOT NULL "
                "AND i.created_at >= now() - make_interval(days => :days) "
                "AND e.created_at >= now() - make_interval(days => :days) "
                "ORDER BY e.embedding <=> CAST(:vec AS vector), e.item_id LIMIT 1"
            ),
            {
                "org": org,
                "id": item,
                "model": embedded.model,
                "dim": embedded.dim,
                "vec": vector,
                "days": window_days,
            },
        )
    ).first()
    # "Not a number" is no similarity: it never joins (a comparison with it is always false, so the
    # check is written as "must reach the threshold", not "must not fall short of it").
    if near is None or not _reaches(near.sim, threshold):
        return False
    cluster = mine.cluster_id or near.cluster_id
    # Joining an existing cluster: it must have room, and the newcomer must be close to its first
    # member too, not only to the nearest one, or a chain of near-copies (A like B, B like C, C like
    # D) would pull a different story into the cluster one step at a time.
    # The same check applies when the newcomer already has a cluster and the neighbour would join
    # it (for example after a change of model without a reset): the cap holds either way. Two
    # articles that both have a cluster are left as they are; clusters are never merged.
    if (
        cluster is not None
        and mine.cluster_id != near.cluster_id
        and (mine.cluster_id is None or near.cluster_id is None)
        and not await _may_join(connection, org, cluster, embedded, vector, threshold=threshold)
    ):
        return False
    if cluster is None:
        cluster = (await connection.execute(text("SELECT gen_random_uuid()"))).scalar_one()
    # An article that already belongs to a cluster keeps it (clusters are never merged); the
    # neighbour joins only if it has none. Each update names the rows it may touch, and only
    # articles that are still public and current.
    await connection.execute(
        text(
            "UPDATE items SET cluster_id = :cluster WHERE org_id = :org "
            "AND id IN (:id, :near) AND cluster_id IS NULL "
            "AND owner_user_id IS NULL AND status = 'new'"
        ),
        {"cluster": cluster, "org": org, "id": item, "near": near.item_id},
    )
    return True


def _reaches(similarity: object, threshold: float) -> bool:
    """True only for a real number at or above the threshold (not NULL, not NaN)."""
    return (
        isinstance(similarity, (int, float))
        and math.isfinite(similarity)
        and similarity >= threshold
    )


async def _may_join(
    connection: AsyncConnection,
    org: uuid.UUID,
    cluster: uuid.UUID,
    embedded: Embedded,
    vector: str,
    *,
    threshold: float,
) -> bool:
    row = (
        await connection.execute(
            text(
                "WITH members AS ("
                "  SELECT i.id, i.created_at FROM items i WHERE i.org_id = :org "
                "  AND i.cluster_id = :cluster AND i.owner_user_id IS NULL AND i.status = 'new') "
                "SELECT (SELECT count(*) FROM members) AS size, "
                "  (SELECT 1 - (e.embedding <=> CAST(:vec AS vector)) FROM members m "
                "   JOIN item_embeddings e ON e.org_id = :org AND e.item_id = m.id "
                "   AND e.model = :model AND e.dim = :dim AND vector_norm(e.embedding) > 0 "
                "   ORDER BY m.created_at, m.id LIMIT 1) AS to_first"
            ),
            {
                "org": org,
                "cluster": cluster,
                "vec": vector,
                "model": embedded.model,
                "dim": embedded.dim,
            },
        )
    ).one()
    if row.size >= MAX_CLUSTER_SIZE:
        return False
    # A first member without a vector of this model cannot be compared; the nearest one decided.
    return row.to_first is None or _reaches(row.to_first, threshold)


async def reset(
    engine: AsyncEngine, org: uuid.UUID, *, model: str | None = None
) -> tuple[int, int]:
    """Undo the clusters and delete the vectors (of one model, or all), so that the next sweeps
    embed and cluster the recent articles again. Needed after a change of model or threshold, or to
    undo a wrong cluster. Returns how many vectors were deleted and how many articles left a
    cluster. Articles are not touched otherwise."""
    async with engine.begin() as connection:
        await _enter(connection, org)
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
            {"name": f"pharos.embed:{org}"},
        )
        deleted = await connection.execute(
            text(
                "DELETE FROM item_embeddings WHERE org_id = :org "
                "AND (CAST(:model AS text) IS NULL OR model = :model)"
            ),
            {"org": org, "model": model},
        )
        cleared = await connection.execute(
            text(
                "UPDATE items SET cluster_id = NULL WHERE org_id = :org AND cluster_id IS NOT NULL"
            ),
            {"org": org},
        )
        return int(deleted.rowcount or 0), int(cleared.rowcount or 0)


async def status(
    engine: AsyncEngine, org: uuid.UUID, *, model: str | None = None
) -> dict[str, int]:
    """Counts only: vectors, clustered articles, clusters, and the largest cluster. Given the
    configured model, also `other_models`: the vectors of any other model. They are left after a
    change of model without a reset, and so are the clusters they made (F-42)."""
    async with engine.begin() as connection:
        await _enter(connection, org)
        row = (
            await connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM item_embeddings WHERE org_id = :org) AS vectors, "
                    "(SELECT count(*) FROM items WHERE org_id = :org AND cluster_id IS NOT NULL) "
                    "  AS clustered, "
                    "(SELECT count(DISTINCT cluster_id) FROM items WHERE org_id = :org) "
                    "  AS clusters, "
                    "coalesce((SELECT max(n) FROM (SELECT count(*) AS n FROM items "
                    "  WHERE org_id = :org AND cluster_id IS NOT NULL GROUP BY cluster_id) g), 0) "
                    "  AS largest"
                ),
                {"org": org},
            )
        ).one()
        counts = {
            "vectors": int(row.vectors),
            "clustered": int(row.clustered),
            "clusters": int(row.clusters),
            "largest": int(row.largest),
        }
        if model is not None:
            other = await connection.execute(
                text(
                    "SELECT count(*) FROM item_embeddings WHERE org_id = :org AND model <> :model"
                ),
                {"org": org, "model": model},
            )
            counts["other_models"] = int(other.scalar_one())
    return counts


async def sweep(
    engine: AsyncEngine,
    gateway: Embedder,
    org: uuid.UUID,
    *,
    model: str,
    batch: int,
    threshold: float,
    window_days: int,
) -> SweepResult:
    """Embed up to `batch` articles and place each one. See the module docstring."""
    result = SweepResult()
    async with engine.begin() as connection:
        ids = await pending_items(connection, org, model, window_days, batch)
    in_a_row = 0
    for item in ids:
        try:
            async with engine.begin() as connection:
                article = await load_public_article(connection, org, item)
            embedded = await gateway.embed(org_id=org, article=article)
            async with engine.begin() as connection:
                joined = await place(
                    connection,
                    org,
                    item,
                    embedded,
                    threshold=threshold,
                    window_days=window_days,
                )
        except NotPublicArticle:
            result.skipped += 1  # it changed since the list was made
            continue
        except _STOPS as stop:
            result.stopped = stop.code if hasattr(stop, "code") else type(stop).__name__
            logger.info("embedding sweep ended early", extra={"reason": result.stopped})
            break
        except (ProviderRejected, Exception) as error:
            # One article that cannot be embedded must not stop the others; after three in a row
            # the sweep ends (a wrong model name or a broken server looks the same).
            in_a_row += 1
            result.failed += 1
            logger.warning("embedding of one article failed", extra={"error": type(error).__name__})
            if in_a_row >= MAX_FAILURES_IN_A_ROW:
                result.stopped = "repeated_failures"
                break
            continue
        in_a_row = 0
        if joined is None:
            result.skipped += 1
            continue
        result.embedded += 1
        result.clustered += bool(joined)
    return result
