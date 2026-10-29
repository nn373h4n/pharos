"""The SQL of the ingestion worker. Everything here runs as `pharos_worker`, inside row-level
security, with grants that cover the ingestion tables only (migration 0003).

Time is always passed in, never read from the database, so the scheduling logic can be tested with
a clock that moves when the test says so.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from pharos.ingest.feed import RawEntry


@dataclass(frozen=True, slots=True)
class Leased:
    id: uuid.UUID
    lease_token: uuid.UUID


@dataclass(frozen=True, slots=True)
class SourceRow:
    id: uuid.UUID
    type: str
    config: dict[str, Any]
    interval_seconds: int
    etag: str | None
    last_modified: str | None
    consecutive_failures: int


async def resolve_org(connection: AsyncConnection, slug: str) -> uuid.UUID | None:
    """The organization of this installation, found the only way the worker may: by its slug."""
    row = await connection.execute(text("SELECT pharos_org_id_for_slug(:slug)"), {"slug": slug})
    found = row.scalar_one()
    return found if isinstance(found, uuid.UUID) else None


async def enter_org(connection: AsyncConnection, org: uuid.UUID) -> None:
    await connection.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org)})


async def count_in_flight(connection: AsyncConnection, now: datetime) -> int:
    row = await connection.execute(
        text("SELECT count(*) FROM sources WHERE lease_until > :now"), {"now": now}
    )
    return int(row.scalar_one())


async def lease_due_sources(
    connection: AsyncConnection, now: datetime, lease_until: datetime, limit: int
) -> list[Leased]:
    """Take a lease on up to `limit` sources that are due. Two dispatchers never get the same one:
    `SKIP LOCKED` hides rows the other has locked, and a leased source is no longer due."""
    if limit <= 0:
        return []
    rows = await connection.execute(
        text(
            "WITH due AS ("
            "  SELECT id FROM sources"
            "  WHERE enabled AND next_fetch_at <= :now"
            "    AND (lease_until IS NULL OR lease_until <= :now)"
            "  ORDER BY (owner_user_id IS NOT NULL), next_fetch_at, id"
            "  LIMIT :limit FOR UPDATE SKIP LOCKED"
            ") UPDATE sources s SET lease_token = gen_random_uuid(), lease_until = :until "
            "FROM due WHERE s.id = due.id RETURNING s.id, s.lease_token"
        ),
        {"now": now, "until": lease_until, "limit": limit},
    )
    return [Leased(row.id, row.lease_token) for row in rows]


async def release_lease(connection: AsyncConnection, source: uuid.UUID, token: uuid.UUID) -> None:
    await connection.execute(
        text(
            "UPDATE sources SET lease_token = NULL, lease_until = NULL "
            "WHERE id = :id AND lease_token = :token"
        ),
        {"id": source, "token": token},
    )


async def load_leased(
    connection: AsyncConnection, source: uuid.UUID, token: uuid.UUID, now: datetime
) -> SourceRow | None:
    """The source, if this lease is still the live one and the source is still enabled."""
    row = (
        await connection.execute(
            text(
                "SELECT id, type, config, interval_seconds, etag, last_modified, "
                "consecutive_failures FROM sources "
                "WHERE id = :id AND lease_token = :token AND lease_until > :now AND enabled"
            ),
            {"id": source, "token": token, "now": now},
        )
    ).first()
    if row is None:
        return None
    return SourceRow(
        row.id,
        row.type,
        dict(row.config),
        row.interval_seconds,
        row.etag,
        row.last_modified,
        row.consecutive_failures,
    )


async def defer(
    connection: AsyncConnection,
    source: uuid.UUID,
    token: uuid.UUID,
    next_fetch_at: datetime,
) -> None:
    """Give the source back without a fetch (the host was just visited): try again later."""
    await connection.execute(
        text(
            "UPDATE sources SET lease_token = NULL, lease_until = NULL, next_fetch_at = :next "
            "WHERE id = :id AND lease_token = :token"
        ),
        {"id": source, "token": token, "next": next_fetch_at},
    )


async def start_run(
    connection: AsyncConnection,
    org: uuid.UUID,
    source: uuid.UUID,
    token: uuid.UUID,
    now: datetime,
    min_lease_left: timedelta,
) -> tuple[int, uuid.UUID] | None:
    """Record that a fetch begins and swap the lease token for a fresh one.

    Only the task holding the current token gets through, so of two copies of the same task exactly
    one fetches; the other finds a token that no longer exists. A lease that is about to run out is
    refused too: the fetch could not finish inside it. Returns the run and the new token, or None.
    """
    owned = await connection.execute(
        text(
            "UPDATE sources SET last_fetched_at = :now, lease_token = gen_random_uuid() "
            "WHERE id = :id AND lease_token = :token AND lease_until > :earliest "
            "RETURNING lease_token"
        ),
        {"id": source, "token": token, "now": now, "earliest": now + min_lease_left},
    )
    fresh = owned.first()
    if fresh is None:
        return None
    row = await connection.execute(
        text(
            "INSERT INTO fetch_runs (org_id, source_id, outcome) VALUES (:org, :id, 'running') "
            "RETURNING id"
        ),
        {"org": org, "id": source},
    )
    return int(row.scalar_one()), fresh.lease_token


async def store_raw_items(
    connection: AsyncConnection, org: uuid.UUID, source: uuid.UUID, entries: Sequence[RawEntry]
) -> int:
    """Store what the source said. An entry already stored unchanged is left alone; one that has
    changed replaces the old copy (the worker may delete and insert, but not update, raw items).
    Returns how many rows are new or changed; running it twice returns 0 the second time."""
    if not entries:
        return 0
    arrays = {
        "ids": [e.external_id for e in entries],
        "titles": [e.title for e in entries],
        "excerpts": [e.excerpt for e in entries],
        "urls": [e.url for e in entries],
        "published": [e.published_at for e in entries],
        "hashes": [e.content_sha256 for e in entries],
    }
    await connection.execute(
        text(
            "DELETE FROM raw_items r USING unnest(CAST(:ids AS text[]), CAST(:hashes AS text[])) "
            "AS i(external_id, hash) WHERE r.org_id = :org AND r.source_id = :source "
            "AND r.external_id = i.external_id AND r.content_sha256 <> i.hash"
        ),
        {**arrays, "org": org, "source": source},
    )
    row = await connection.execute(
        text(
            "INSERT INTO raw_items (org_id, source_id, external_id, title, excerpt, url, "
            "published_at, content_sha256) "
            "SELECT :org, :source, i.external_id, i.title, i.excerpt, i.url, i.published, i.hash "
            "FROM unnest(CAST(:ids AS text[]), CAST(:titles AS text[]), CAST(:excerpts AS text[]), "
            "CAST(:urls AS text[]), CAST(:published AS timestamptz[]), CAST(:hashes AS text[])) "
            "AS i(external_id, title, excerpt, url, published, hash) "
            "ON CONFLICT (org_id, source_id, external_id) DO NOTHING RETURNING id"
        ),
        {**arrays, "org": org, "source": source},
    )
    return len(row.all())


async def finish_run(
    connection: AsyncConnection,
    run: int,
    *,
    outcome: str,
    now: datetime,
    started: datetime,
    error_code: str | None = None,
    http_status: int | None = None,
    bytes_fetched: int | None = None,
    items_seen: int | None = None,
    items_new: int | None = None,
) -> None:
    duration = max(int((now - started).total_seconds() * 1000), 0)
    await connection.execute(
        text(
            "UPDATE fetch_runs SET finished_at = :now, outcome = :outcome, error_code = :code, "
            "http_status = :status, duration_ms = :ms, bytes_fetched = :bytes, "
            "items_seen = :seen, items_new = :new WHERE id = :id"
        ),
        {
            "now": now,
            "outcome": outcome,
            "code": error_code,
            "status": http_status,
            "ms": duration,
            "bytes": bytes_fetched,
            "seen": items_seen,
            "new": items_new,
            "id": run,
        },
    )


async def record_success(
    connection: AsyncConnection,
    source: uuid.UUID,
    token: uuid.UUID,
    *,
    now: datetime,
    next_fetch_at: datetime,
    etag: str | None,
    last_modified: str | None,
) -> bool:
    """Close the lease and remember the result. False if the lease is no longer ours."""
    row = await connection.execute(
        text(
            "UPDATE sources SET lease_token = NULL, lease_until = NULL, etag = :etag, "
            "last_modified = :modified, consecutive_failures = 0, last_error_code = NULL, "
            "last_success_at = :now, next_fetch_at = :next "
            "WHERE id = :id AND lease_token = :token RETURNING id"
        ),
        {
            "id": source,
            "token": token,
            "etag": etag,
            "modified": last_modified,
            "now": now,
            "next": next_fetch_at,
        },
    )
    return row.first() is not None


async def record_failure(
    connection: AsyncConnection,
    source: uuid.UUID,
    token: uuid.UUID,
    *,
    error_code: str,
    next_fetch_at: datetime,
) -> int | None:
    """Close the lease after a failure. Returns the new failure count, None if not ours."""
    row = await connection.execute(
        text(
            "UPDATE sources SET lease_token = NULL, lease_until = NULL, "
            "consecutive_failures = consecutive_failures + 1, last_error_code = :code, "
            "next_fetch_at = :next WHERE id = :id AND lease_token = :token "
            "RETURNING consecutive_failures"
        ),
        {"id": source, "token": token, "code": error_code, "next": next_fetch_at},
    )
    found = row.first()
    return None if found is None else int(found[0])


@dataclass(frozen=True, slots=True)
class Retention:
    fetch_runs_days: int = 30
    raw_items_days: int = 14
    items_days: int = 180
    stuck_run_hours: int = 1


async def cleanup(
    connection: AsyncConnection, now: datetime, retention: Retention
) -> dict[str, int]:
    """Delete what is past its retention. Held items are kept for a person to decide; hidden items
    are kept as a record that the article was rejected (the database refuses the worker to delete
    them anyway). A run that has been 'running' for too long was abandoned, e.g. its worker died."""
    abandoned = await connection.execute(
        text(
            "UPDATE fetch_runs SET outcome = 'error', error_code = 'abandoned', finished_at = :now "
            "WHERE outcome = 'running' AND started_at < :cutoff RETURNING id"
        ),
        {"now": now, "cutoff": now - timedelta(hours=retention.stuck_run_hours)},
    )
    runs = await connection.execute(
        text("DELETE FROM fetch_runs WHERE started_at < :cutoff RETURNING id"),
        {"cutoff": now - timedelta(days=retention.fetch_runs_days)},
    )
    raw = await connection.execute(
        text("DELETE FROM raw_items WHERE fetched_at < :cutoff RETURNING id"),
        {"cutoff": now - timedelta(days=retention.raw_items_days)},
    )
    items = await connection.execute(
        text("DELETE FROM items WHERE fetched_at < :cutoff AND status = 'new' RETURNING id"),
        {"cutoff": now - timedelta(days=retention.items_days)},
    )
    return {
        "abandoned_runs": len(abandoned.all()),
        "fetch_runs": len(runs.all()),
        "raw_items": len(raw.all()),
        "items": len(items.all()),
    }
