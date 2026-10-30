"""SQL for a person's own feeds.

Only the feeds of one person (`owner_user_id = :user`) are touched, in every statement, next to the
row-level policy that hides other people's feeds: the application names the person for the
transaction (`app.user_id`), and a statement here still names them again. The organization's own
sources (`owner_user_id IS NULL`) belong to `pharos.sources` and are never touched here.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_VIEW = (
    "id, name, config, enabled, interval_seconds, next_fetch_at, last_fetched_at, "
    "last_success_at, consecutive_failures, last_error_code, created_at, updated_at"
)


@dataclass(frozen=True, slots=True)
class FeedView:
    id: uuid.UUID
    name: str
    url: str
    enabled: bool
    interval_seconds: int
    next_fetch_at: datetime
    last_fetched_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    dedupe_key: str = ""


def _view(row: Any) -> FeedView:
    values = dict(row._mapping)
    config = values.pop("config")
    return FeedView(url=str(dict(config).get("url", "")), **values)


async def act_as(session: AsyncSession, user: uuid.UUID) -> None:
    """Name the person for this transaction, so the database shows their feeds and nobody else's.
    It lasts until the transaction ends: call it again after a commit or a release."""
    await session.execute(
        text("SELECT set_config('app.user_id', :user, true)"), {"user": str(user)}
    )


async def list_feeds(
    session: AsyncSession, org: uuid.UUID, user: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[FeedView], int]:
    rows = (
        await session.execute(
            text(
                f"SELECT {_VIEW}, dedupe_key FROM sources "  # noqa: S608 (fixed fragments only)
                "WHERE org_id = :org AND owner_user_id = :user "
                "ORDER BY created_at, id LIMIT :limit OFFSET :offset"
            ),
            {"org": org, "user": user, "limit": limit, "offset": offset},
        )
    ).all()
    total = await count_feeds(session, org, user)
    return [_view(r) for r in rows], total


async def count_feeds(session: AsyncSession, org: uuid.UUID, user: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM sources WHERE org_id = :org AND owner_user_id = :user"),
                {"org": org, "user": user},
            )
        ).scalar_one()
    )


async def lock_user_feeds(session: AsyncSession, org: uuid.UUID, user: uuid.UUID) -> None:
    """One creation at a time per person until the end of the transaction."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
        {"name": f"feeds:{org}:{user}"},
    )


async def get_feed(
    session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    feed: uuid.UUID,
    *,
    lock: bool = False,
) -> FeedView | None:
    row = (
        await session.execute(
            text(
                f"SELECT {_VIEW}, dedupe_key FROM sources "  # noqa: S608 (fixed fragments only)
                "WHERE org_id = :org AND owner_user_id = :user AND id = :id"
                + (" FOR UPDATE" if lock else "")
            ),
            {"org": org, "user": user, "id": feed},
        )
    ).first()
    return None if row is None else _view(row)


async def key_taken(
    session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    dedupe_key: str,
    *,
    except_id: uuid.UUID | None = None,
) -> bool:
    row = await session.execute(
        text(
            "SELECT 1 FROM sources WHERE org_id = :org AND owner_user_id = :user "
            "AND dedupe_key = :key AND (CAST(:except AS uuid) IS NULL OR id <> :except)"
        ),
        {"org": org, "user": user, "key": dedupe_key, "except": except_id},
    )
    return row.first() is not None


async def insert_feed(
    session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    *,
    name: str,
    config: dict[str, Any],
    dedupe_key: str,
    enabled: bool,
    interval_seconds: int,
) -> FeedView | None:
    """The new feed, or None if the person has the same feed already."""
    row = (
        await session.execute(
            text(
                "INSERT INTO sources (org_id, owner_user_id, type, name, config, dedupe_key, "  # noqa: S608 (only the fixed column list is inserted)
                "enabled, interval_seconds, created_by) VALUES (:org, :user, 'rss', :name, "
                "CAST(:config AS jsonb), :key, :enabled, :interval, :user) "
                f"ON CONFLICT DO NOTHING RETURNING {_VIEW}, dedupe_key"
            ),
            {
                "org": org,
                "user": user,
                "name": name,
                "config": json.dumps(config),
                "key": dedupe_key,
                "enabled": enabled,
                "interval": interval_seconds,
            },
        )
    ).first()
    return None if row is None else _view(row)


async def update_feed(
    session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    feed: uuid.UUID,
    *,
    name: str,
    config: dict[str, Any],
    dedupe_key: str,
    enabled: bool,
    interval_seconds: int,
) -> FeedView:
    row = (
        await session.execute(
            text(
                "UPDATE sources SET name = :name, config = CAST(:config AS jsonb), "  # noqa: S608 (only the fixed column list is changed)
                "dedupe_key = :key, enabled = :enabled, interval_seconds = :interval "
                "WHERE org_id = :org AND owner_user_id = :user AND id = :id "
                f"RETURNING {_VIEW}, dedupe_key"
            ),
            {
                "org": org,
                "user": user,
                "id": feed,
                "name": name,
                "config": json.dumps(config),
                "key": dedupe_key,
                "enabled": enabled,
                "interval": interval_seconds,
            },
        )
    ).one()
    return _view(row)


async def delete_feed(
    session: AsyncSession, org: uuid.UUID, user: uuid.UUID, feed: uuid.UUID
) -> bool:
    """Delete the feed with its runs, raw entries and links (the database cascades). The articles
    stay in the table, hidden from everybody, until retention removes them."""
    row = await session.execute(
        text(
            "DELETE FROM sources WHERE org_id = :org AND owner_user_id = :user AND id = :id "
            "RETURNING id"
        ),
        {"org": org, "user": user, "id": feed},
    )
    return row.first() is not None


async def recent_runs(
    session: AsyncSession, org: uuid.UUID, user: uuid.UUID, feed: uuid.UUID, *, limit: int
) -> list[Any]:
    """The latest runs of a feed of this person."""
    return list(
        (
            await session.execute(
                text(
                    "SELECT r.id, r.started_at, r.finished_at, r.outcome, r.error_code, "
                    "r.http_status, r.duration_ms, r.bytes_fetched, r.items_seen, r.items_new "
                    "FROM fetch_runs r JOIN sources s "
                    "ON s.org_id = r.org_id AND s.id = r.source_id "
                    "WHERE r.org_id = :org AND r.source_id = :id AND s.owner_user_id = :user "
                    "ORDER BY r.started_at DESC, r.id DESC LIMIT :limit"
                ),
                {"org": org, "id": feed, "user": user, "limit": limit},
            )
        ).all()
    )


async def list_items(
    session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    feed: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[Any], int]:
    """The articles of a feed of this person, newest first."""
    where = (
        "i.org_id = :org AND i.owner_user_id = :user AND EXISTS (SELECT 1 FROM item_sources l "
        "WHERE l.org_id = i.org_id AND l.item_id = i.id AND l.source_id = :feed)"
    )
    params = {"org": org, "user": user, "feed": feed}
    rows = (
        await session.execute(
            text(
                "SELECT i.id, i.canonical_url AS url, i.title, i.excerpt, i.lang, "  # noqa: S608 (fixed fragments only)
                "i.published_at, i.fetched_at, i.status, i.hold_reason FROM items i "
                f"WHERE {where} "
                "ORDER BY coalesce(i.published_at, i.fetched_at) DESC, i.id DESC "
                "LIMIT :limit OFFSET :offset"
            ),
            {**params, "limit": limit, "offset": offset},
        )
    ).all()
    total = (
        await session.execute(text(f"SELECT count(*) FROM items i WHERE {where}"), params)  # noqa: S608
    ).scalar_one()
    return list(rows), int(total)
