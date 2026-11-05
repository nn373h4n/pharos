"""SQL for the approved AI tools, as the application role. Every statement carries `org_id`;
row-level security is the second line. The application role may change only name and address."""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_VIEW = "id, name, url"
_SELECT = f"SELECT {_VIEW} FROM ai_tools WHERE org_id = :org"  # noqa: S608 (fixed columns)


@dataclass(frozen=True, slots=True)
class AiToolView:
    id: uuid.UUID
    name: str
    url: str


def _view(row: Any) -> AiToolView:
    return AiToolView(**dict(row._mapping))


async def lock_org_tools(session: AsyncSession, org: uuid.UUID) -> None:
    """One change at a time per organization until the end of the transaction, so the cap on the
    number of tools is exact. The database trigger takes the same lock."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
        {"name": f"ai_tools:{org}"},
    )


async def list_tools(session: AsyncSession, org: uuid.UUID) -> list[AiToolView]:
    rows = await session.execute(
        text(_SELECT + " ORDER BY lower(name), id"),
        {"org": org},
    )
    return [_view(r) for r in rows]


async def count_tools(session: AsyncSession, org: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM ai_tools WHERE org_id = :org"), {"org": org}
            )
        ).scalar_one()
    )


async def get_tool(session: AsyncSession, org: uuid.UUID, tool_id: uuid.UUID) -> AiToolView | None:
    row = (
        await session.execute(
            text(_SELECT + " AND id = :id"),
            {"org": org, "id": tool_id},
        )
    ).first()
    return _view(row) if row else None


async def insert_tool(
    session: AsyncSession, org: uuid.UUID, *, name: str, url: str, created_by: uuid.UUID
) -> AiToolView | None:
    """The new tool, or None if a tool of that name exists already."""
    row = (
        await session.execute(
            text(
                "INSERT INTO ai_tools (org_id, name, url, created_by) "  # noqa: S608 (fixed columns)
                "VALUES (:org, :name, :url, :by) ON CONFLICT DO NOTHING "
                f"RETURNING {_VIEW}"
            ),
            {"org": org, "name": name, "url": url, "by": created_by},
        )
    ).first()
    return _view(row) if row else None


async def update_tool(
    session: AsyncSession,
    org: uuid.UUID,
    tool_id: uuid.UUID,
    *,
    name: str | None,
    url: str | None,
) -> AiToolView | None:
    """The changed tool, or None if it does not exist. A taken name raises IntegrityError."""
    row = (
        await session.execute(
            text(
                "UPDATE ai_tools SET name = coalesce(CAST(:name AS text), name), "  # noqa: S608 (fixed columns)
                "url = coalesce(CAST(:url AS text), url), updated_at = now() "
                f"WHERE org_id = :org AND id = :id RETURNING {_VIEW}"
            ),
            {"org": org, "id": tool_id, "name": name, "url": url},
        )
    ).first()
    return _view(row) if row else None


async def delete_tool(session: AsyncSession, org: uuid.UUID, tool_id: uuid.UUID) -> bool:
    deleted = await session.execute(
        text("DELETE FROM ai_tools WHERE org_id = :org AND id = :id"),
        {"org": org, "id": tool_id},
    )
    return bool(deleted.rowcount)  # type: ignore[attr-defined]
