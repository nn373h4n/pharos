"""SQL for the organization's items, as the application role.

Only shared items (`owner_user_id IS NULL`) are touched, in every statement: what a person follows
privately is not part of what the organization reads or reviews. The application role may change
`status` and `hold_reason` of an item and nothing else; it cannot create or delete one.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_VIEW = (
    "i.id, i.canonical_url AS url, i.title, i.excerpt, i.lang, i.published_at, i.fetched_at, "
    "i.status, i.hold_reason, i.updated_at"
)


@dataclass(frozen=True, slots=True)
class ItemView:
    id: uuid.UUID
    url: str
    title: str
    excerpt: str
    lang: str | None
    published_at: datetime | None
    fetched_at: datetime
    status: str
    hold_reason: str | None
    updated_at: datetime


def _view(row: Any) -> ItemView:
    return ItemView(**dict(row._mapping))


async def list_items(
    session: AsyncSession,
    org: uuid.UUID,
    *,
    status: str | None,
    source: uuid.UUID | None,
    limit: int,
    offset: int,
) -> tuple[list[ItemView], int]:
    where = "i.org_id = :org AND i.owner_user_id IS NULL"
    params: dict[str, Any] = {"org": org}
    if status is not None:
        where += " AND i.status = :status"
        params["status"] = status
    if source is not None:
        where += (
            " AND EXISTS (SELECT 1 FROM item_sources l WHERE l.org_id = i.org_id "
            "AND l.item_id = i.id AND l.source_id = :source)"
        )
        params["source"] = source
    rows = (
        await session.execute(
            text(
                f"SELECT {_VIEW} FROM items i WHERE {where} "  # noqa: S608 (fixed fragments only)
                "ORDER BY coalesce(i.published_at, i.fetched_at) DESC, i.id DESC "
                "LIMIT :limit OFFSET :offset"
            ),
            {**params, "limit": limit, "offset": offset},
        )
    ).all()
    total = (
        await session.execute(text(f"SELECT count(*) FROM items i WHERE {where}"), params)  # noqa: S608
    ).scalar_one()
    return [_view(r) for r in rows], int(total)


async def get_item(
    session: AsyncSession, org: uuid.UUID, item: uuid.UUID, *, lock: bool = False
) -> ItemView | None:
    """The item, or None. With `lock` the row is held until the end of the transaction, so two
    editors acting on the same item queue up and the second sees what the first left behind."""
    row = (
        await session.execute(
            text(
                f"SELECT {_VIEW} FROM items i "  # noqa: S608
                "WHERE i.org_id = :org AND i.id = :id AND i.owner_user_id IS NULL"
                + (" FOR UPDATE" if lock else "")
            ),
            {"org": org, "id": item},
        )
    ).first()
    return None if row is None else _view(row)


async def set_status(
    session: AsyncSession,
    org: uuid.UUID,
    item: uuid.UUID,
    *,
    status: str,
    hold_reason: str | None,
) -> ItemView:
    row = (
        await session.execute(
            text(
                "UPDATE items i SET status = :status, hold_reason = :reason, "  # noqa: S608
                "reviewed_at = COALESCE(i.reviewed_at, now()) "
                "WHERE i.org_id = :org AND i.id = :id AND i.owner_user_id IS NULL "
                f"RETURNING {_VIEW}"
            ),
            {"org": org, "id": item, "status": status, "reason": hold_reason},
        )
    ).one()
    return _view(row)


async def sources_of(
    session: AsyncSession, org: uuid.UUID, items: list[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[uuid.UUID, str]]]:
    """Which organization sources reported each item (a person's own feeds are left out)."""
    if not items:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT l.item_id, s.id, s.name FROM item_sources l "
                "JOIN sources s ON s.org_id = l.org_id AND s.id = l.source_id "
                "WHERE l.org_id = :org AND l.item_id = ANY(:ids) AND s.owner_user_id IS NULL "
                "ORDER BY lower(s.name), s.id"
            ),
            {"org": org, "ids": items},
        )
    ).all()
    found: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = {}
    for row in rows:
        found.setdefault(row.item_id, []).append((row.id, row.name))
    return found
