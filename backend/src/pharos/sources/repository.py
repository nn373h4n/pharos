"""SQL for the organization's sources, as the application role.

Only organization sources (`owner_user_id IS NULL`) are touched here, in every statement: a person's
own feeds belong to that person and are not managed by administrators. The application role may
change only the configuration columns of a source; the fetch state is the worker's.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_VIEW = (
    "id, type, name, config, enabled, interval_seconds, next_fetch_at, last_fetched_at, "
    "last_success_at, consecutive_failures, last_error_code, created_by, created_at, updated_at"
)


@dataclass(frozen=True, slots=True)
class SourceView:
    id: uuid.UUID
    type: str
    name: str
    config: dict[str, Any]
    enabled: bool
    interval_seconds: int
    next_fetch_at: datetime
    last_fetched_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error_code: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    dedupe_key: str = ""


def _view(row: Any) -> SourceView:
    values = dict(row._mapping)
    values["config"] = dict(values["config"])
    return SourceView(**values)


async def act_as(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Name the person for this transaction, so the database can check who is writing (a source is
    credited to nobody or to the person making the request)."""
    await session.execute(
        text("SELECT set_config('app.user_id', :user, true)"), {"user": str(user_id)}
    )


async def list_sources(
    session: AsyncSession,
    org: uuid.UUID,
    *,
    limit: int,
    offset: int,
    source_type: str | None,
    enabled: bool | None,
) -> tuple[list[SourceView], int]:
    where = "org_id = :org AND owner_user_id IS NULL"
    params: dict[str, Any] = {"org": org, "limit": limit, "offset": offset}
    if source_type is not None:
        where += " AND type = :type"
        params["type"] = source_type
    if enabled is not None:
        where += " AND enabled = :enabled"
        params["enabled"] = enabled
    rows = (
        await session.execute(
            text(
                f"SELECT {_VIEW} FROM sources WHERE {where} "  # noqa: S608 (only the fixed column list is inserted)
                "ORDER BY lower(name), id LIMIT :limit OFFSET :offset"
            ),
            params,
        )
    ).all()
    total = (
        await session.execute(
            text(f"SELECT count(*) FROM sources WHERE {where}"),  # noqa: S608
            {k: v for k, v in params.items() if k not in ("limit", "offset")},
        )
    ).scalar_one()
    return [_view(r) for r in rows], int(total)


async def lock_org_sources(session: AsyncSession, org: uuid.UUID) -> None:
    """One creation at a time per organization until the end of the transaction, so the cap on the
    number of sources is exact and not "about"."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
        {"name": f"sources:{org}"},
    )


async def count_sources(session: AsyncSession, org: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM sources WHERE org_id = :org AND owner_user_id IS NULL"),
                {"org": org},
            )
        ).scalar_one()
    )


async def get_source(
    session: AsyncSession, org: uuid.UUID, source: uuid.UUID, *, lock: bool = False
) -> SourceView | None:
    """The source, or None. With `lock` the row is held until the end of the transaction, so two
    administrators changing the same source queue up."""
    row = (
        await session.execute(
            text(
                f"SELECT {_VIEW}, dedupe_key FROM sources "  # noqa: S608
                "WHERE org_id = :org AND id = :id AND owner_user_id IS NULL"
                + (" FOR UPDATE" if lock else "")
            ),
            {"org": org, "id": source},
        )
    ).first()
    return None if row is None else _view(row)


async def key_taken(
    session: AsyncSession, org: uuid.UUID, dedupe_key: str, *, except_id: uuid.UUID | None = None
) -> bool:
    row = await session.execute(
        text(
            "SELECT 1 FROM sources WHERE org_id = :org AND owner_user_id IS NULL "
            "AND dedupe_key = :key AND (CAST(:except AS uuid) IS NULL OR id <> :except)"
        ),
        {"org": org, "key": dedupe_key, "except": except_id},
    )
    return row.first() is not None


async def insert_source(
    session: AsyncSession,
    org: uuid.UUID,
    *,
    source_type: str,
    name: str,
    config: dict[str, Any],
    dedupe_key: str,
    enabled: bool,
    interval_seconds: int,
    created_by: uuid.UUID,
) -> SourceView | None:
    """The new source, or None if the same source exists already."""
    row = (
        await session.execute(
            text(
                "INSERT INTO sources (org_id, type, name, config, dedupe_key, enabled, "  # noqa: S608 (only the fixed column list is inserted)
                "interval_seconds, created_by) VALUES (:org, :type, :name, CAST(:config AS jsonb), "
                f":key, :enabled, :interval, :by) ON CONFLICT DO NOTHING RETURNING {_VIEW}, "
                "dedupe_key"
            ),
            {
                "org": org,
                "type": source_type,
                "name": name,
                "config": json.dumps(config),
                "key": dedupe_key,
                "enabled": enabled,
                "interval": interval_seconds,
                "by": created_by,
            },
        )
    ).first()
    return None if row is None else _view(row)


async def update_source(
    session: AsyncSession,
    org: uuid.UUID,
    source: uuid.UUID,
    *,
    name: str,
    config: dict[str, Any],
    dedupe_key: str,
    enabled: bool,
    interval_seconds: int,
) -> SourceView:
    row = (
        await session.execute(
            text(
                "UPDATE sources SET name = :name, config = CAST(:config AS jsonb), "  # noqa: S608 (only the fixed column list is inserted)
                "dedupe_key = :key, enabled = :enabled, interval_seconds = :interval "
                "WHERE org_id = :org AND id = :id AND owner_user_id IS NULL "
                f"RETURNING {_VIEW}, dedupe_key"
            ),
            {
                "org": org,
                "id": source,
                "name": name,
                "config": json.dumps(config),
                "key": dedupe_key,
                "enabled": enabled,
                "interval": interval_seconds,
            },
        )
    ).one()
    return _view(row)


async def delete_source(session: AsyncSession, org: uuid.UUID, source: uuid.UUID) -> bool:
    """Delete the source and, with it, its runs, raw entries and links (the database cascades).
    The items stay: they are the content, and retention removes them in time."""
    row = await session.execute(
        text(
            "DELETE FROM sources WHERE org_id = :org AND id = :id AND owner_user_id IS NULL "
            "RETURNING id"
        ),
        {"org": org, "id": source},
    )
    return row.first() is not None


async def recent_runs(
    session: AsyncSession, org: uuid.UUID, source: uuid.UUID, *, limit: int
) -> list[Any]:
    return list(
        (
            await session.execute(
                text(
                    "SELECT id, started_at, finished_at, outcome, error_code, http_status, "
                    "duration_ms, bytes_fetched, items_seen, items_new FROM fetch_runs "
                    "WHERE org_id = :org AND source_id = :id "
                    "ORDER BY started_at DESC, id DESC LIMIT :limit"
                ),
                {"org": org, "id": source, "limit": limit},
            )
        ).all()
    )
