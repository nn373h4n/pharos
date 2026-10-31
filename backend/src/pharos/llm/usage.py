"""The record of what each call used. Numbers and words from a fixed list; never a prompt, an
answer, an article, a person or a key (ADR-0016)."""

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

OUTCOMES = ("ok", "rejected", "failed", "refused")


@dataclass(frozen=True, slots=True)
class UsageRecord:
    org_id: uuid.UUID
    stage: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_micro_usd: int
    outcome: str


class UsageSink(Protocol):
    async def record(self, usage: UsageRecord) -> None: ...


class DatabaseUsageSink:
    """Writes to `llm_usage` as the worker role, in a transaction of its own that names the
    organization (row-level security applies to it like to everything else)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, usage: UsageRecord) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.org_id', :org, true)"), {"org": str(usage.org_id)}
            )
            await connection.execute(
                text(
                    "INSERT INTO llm_usage (org_id, stage, provider, model, tokens_in, tokens_out, "
                    "cost_micro_usd, outcome) VALUES (:org, :stage, :provider, :model, :tin, "
                    ":tout, :cost, :outcome)"
                ),
                {
                    "org": usage.org_id,
                    "stage": usage.stage,
                    "provider": usage.provider,
                    "model": usage.model,
                    "tin": usage.tokens_in,
                    "tout": usage.tokens_out,
                    "cost": usage.cost_micro_usd,
                    "outcome": usage.outcome,
                },
            )
