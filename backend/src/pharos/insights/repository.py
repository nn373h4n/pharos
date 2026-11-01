"""The SQL of the insight endpoint: one article the person may read, and the cached insight."""

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class InsightRow:
    status: str
    summary: str | None
    how_to_apply: list[str] | None
    try_prompt: str | None
    safety_note: str | None


async def item_is_readable(session: AsyncSession, org: uuid.UUID, item: uuid.UUID) -> bool:
    """An organization article that is current: the only kind a model has read and a person may
    be shown. A held, hidden or personal article, or another organization's, is not."""
    row = await session.execute(
        text(
            "SELECT 1 FROM items WHERE org_id = :org AND id = :id "
            "AND owner_user_id IS NULL AND status = 'new'"
        ),
        {"org": org, "id": item},
    )
    return row.first() is not None


async def get_insight(
    session: AsyncSession,
    org: uuid.UUID,
    item: uuid.UUID,
    profession: str,
    language: str,
    version: str,
) -> InsightRow | None:
    row = (
        await session.execute(
            text(
                "SELECT status, summary, how_to_apply, try_prompt, safety_note FROM item_insights "
                "WHERE org_id = :org AND item_id = :item AND profession_code = :p "
                "AND language = :l AND prompt_version = :v "
                # A rejection is remembered for a day, then the worker may try again.
                "AND NOT (status = 'rejected' AND created_at < now() - interval '24 hours')"
            ),
            {"org": org, "item": item, "p": profession, "l": language, "v": version},
        )
    ).first()
    if row is None:
        return None
    steps = row.how_to_apply
    if isinstance(steps, str):
        steps = json.loads(steps)
    return InsightRow(row.status, row.summary, steps, row.try_prompt, row.safety_note)
