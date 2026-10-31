"""Pieces the model-layer tests share: an in-memory budget that behaves like the Redis one, a usage
sink that remembers, and a gateway built around the fake provider."""

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from pharos.llm.budget import Refusal, Reservation
from pharos.llm.gateway import GatewayConfig, LLMGateway
from pharos.llm.provider import FakeProvider
from pharos.llm.types import TASK_STAGE, PublicArticleText, Stage, mint_public_text
from pharos.llm.usage import UsageRecord

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    score: int


GOOD = '{"summary": "A short text.", "score": 4}'


def article(**changes: object) -> PublicArticleText:
    """Public text as the loader would make it. Tests may mint; the product does not (see the
    test that reads the source)."""
    fields: dict[str, object] = {
        "title": "Using AI to reconcile ledgers",
        "excerpt": "A practical walk-through for finance teams.",
        "canonical_url": "https://news.example.org/story",
        "language": "en",
        **changes,
    }
    return mint_public_text(
        str(fields["title"]),
        str(fields["excerpt"]),
        str(fields["canonical_url"]),
        fields["language"],  # type: ignore[arg-type]
    )


class FixedClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current


class MemoryBudget:
    """The interface of `Budget`, in memory, so gateway tests need no Redis."""

    def __init__(self, cap: int = 10**12, calls_per_minute: int = 10**6) -> None:
        self.cap = cap
        self.calls_per_minute = calls_per_minute
        self.total = 0
        self.calls = 0
        self.warned = False
        self.exhausted = False
        self.reserved: list[int] = []
        self.settled: list[int] = []
        self.released = 0

    async def reserve(self, org: uuid.UUID, estimate: int) -> Reservation | Refusal:
        if self.total + estimate > self.cap:
            first = not self.exhausted
            self.exhausted = True
            return Refusal(exhausted_first=first)
        self.total += estimate
        self.reserved.append(estimate)
        warn = not self.warned and self.total * 100 >= self.cap * 80
        self.warned = self.warned or warn
        return Reservation(org, "key", estimate, self.total, warn)

    async def settle(self, reservation: Reservation, actual: int) -> None:
        self.total += actual - reservation.amount
        self.settled.append(actual)

    async def release(self, reservation: Reservation) -> None:
        self.total -= reservation.amount
        self.released += 1

    async def pace(self, org: uuid.UUID, calls_per_minute: int) -> bool:
        self.calls += 1
        return self.calls <= self.calls_per_minute


class MemoryUsage:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, usage: UsageRecord) -> None:
        self.records.append(usage)


class Harness:
    def __init__(
        self,
        provider: FakeProvider | None,
        *,
        budget: MemoryBudget | None = None,
        models: dict[Stage, str] | None = None,
        **config: object,
    ) -> None:
        self.provider = provider
        self.budget = budget or MemoryBudget()
        self.usage = MemoryUsage()
        self.events: list[tuple[str, uuid.UUID]] = []
        self.sleeps: list[float] = []
        self.now = 0.0

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        async def on_event(name: str, org: uuid.UUID) -> None:
            self.events.append((name, org))

        self.gateway = LLMGateway(
            provider,
            self.budget,  # type: ignore[arg-type]
            self.usage,
            GatewayConfig(
                models=models if models is not None else {s: f"model-{s.value}" for s in Stage},
                **config,  # type: ignore[arg-type]
            ),
            FixedClock(),
            sleep=sleep,
            monotonic=lambda: self.now,
            on_event=on_event,
        )

    async def run(self, stage: Stage = Stage.ENRICHMENT, **changes: object) -> Answer:
        task = next(task for task, found in TASK_STAGE.items() if found is stage)
        return await self.gateway.generate(
            task=task, org_id=ORG, article=article(**changes), schema=Answer
        )
