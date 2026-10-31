"""SQL of the model-layer page: the record of calls, summed per day and stage."""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class UsageDay:
    day: date
    stage: str
    calls: int
    refused: int
    failed: int
    tokens_in: int
    tokens_out: int
    cost_micro_usd: int


async def usage_by_day(session: AsyncSession, org: uuid.UUID, *, days: int) -> list[UsageDay]:
    """The last `days` days (UTC), newest first. A refused call costs nothing; a failed one may."""
    rows = await session.execute(
        text(
            "SELECT (created_at AT TIME ZONE 'UTC')::date AS day, stage, "
            "count(*) FILTER (WHERE outcome = 'ok') AS calls, "
            "count(*) FILTER (WHERE outcome = 'refused') AS refused, "
            "count(*) FILTER (WHERE outcome IN ('failed', 'rejected')) AS failed, "
            "coalesce(sum(tokens_in), 0) AS tokens_in, coalesce(sum(tokens_out), 0) AS tokens_out, "
            "coalesce(sum(cost_micro_usd), 0) AS cost FROM llm_usage "
            "WHERE org_id = :org AND created_at >= now() - make_interval(days => :days) "
            "GROUP BY 1, 2 ORDER BY 1 DESC, 2"
        ),
        {"org": org, "days": days},
    )
    return [
        UsageDay(
            day=r.day,
            stage=r.stage,
            calls=int(r.calls),
            refused=int(r.refused),
            failed=int(r.failed),
            tokens_in=int(r.tokens_in),
            tokens_out=int(r.tokens_out),
            cost_micro_usd=int(r.cost),
        )
        for r in rows
    ]
