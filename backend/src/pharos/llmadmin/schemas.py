"""Response bodies of the model-layer endpoints. Nothing in them is secret."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from pharos.llm.keytest import Outcome


class ModelsOut(BaseModel):
    filter: str
    enrichment: str
    insight: str


class UsageDayOut(BaseModel):
    day: date
    stage: Literal["filter", "enrichment", "insight", "key_test"]
    calls: int
    refused: int
    failed: int
    tokens_in: int
    tokens_out: int
    cost_micro_usd: int


class KeyCheckOut(BaseModel):
    outcome: Outcome
    checked_at: datetime


class StatusOut(BaseModel):
    provider: Literal["off", "fake", "anthropic", "openai", "azure"]
    models: ModelsOut
    daily_cap_micro_usd: int
    spent_micro_usd: int
    warned: bool  # the 80% line was crossed today
    exhausted: bool  # the cap was reached today: new summaries are not made
    key_check: KeyCheckOut | None
    usage: list[UsageDayOut]


class KeyTestStartedOut(BaseModel):
    test_id: uuid.UUID
    retry_after_seconds: int


class KeyTestOut(BaseModel):
    status: Literal["pending", "done", "unavailable"]
    outcome: Outcome | None
    checked_at: datetime | None
