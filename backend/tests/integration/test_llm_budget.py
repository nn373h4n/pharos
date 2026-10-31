# ruff: noqa: F811  (the worker_engine fixture is imported by name)
"""The budget against real Redis (as the worker's own Redis user) and the usage record against real
PostgreSQL (as the worker role): atomic, fail-closed and content-free (ADR-0016)."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.llm import schemas
from pharos.llm.budget import KEY_PREFIX, Budget, Refusal, Reservation
from pharos.llm.errors import BudgetExhausted, BudgetUnavailable
from pharos.llm.gateway import GatewayConfig, LLMGateway
from pharos.llm.provider import FakeProvider, ProviderAnswer
from pharos.llm.types import Stage, Task
from pharos.llm.usage import DatabaseUsageSink, UsageRecord

from ..llm.support import GOOD, Answer, FixedClock, article
from .conftest import Orgs
from .ingestion_support import worker_engine  # noqa: F401  (fixture)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
async def redis(redis_worker_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_worker_url)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def _the_test_schema_is_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    for task in Task:
        monkeypatch.setitem(schemas.SCHEMAS, task, Answer)


@pytest.fixture(autouse=True)
async def _remove_usage(owner_engine: AsyncEngine, orgs: Orgs) -> AsyncIterator[None]:
    yield
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM llm_usage WHERE org_id = ANY(:o)"), {"o": list(orgs)}
        )


def budget(redis: Redis, cap: int, clock: FixedClock | None = None) -> Budget:
    return Budget(redis, clock or FixedClock(), daily_cap_micro_usd=cap)


def org() -> uuid.UUID:
    return uuid.uuid4()


class TestReserve:
    async def test_it_books_until_the_cap_and_then_refuses_without_changing_anything(
        self, redis: Redis
    ) -> None:
        b, who = budget(redis, 100), org()
        taken = [await b.reserve(who, 40), await b.reserve(who, 40)]
        assert all(isinstance(r, Reservation) for r in taken) and await b.spent(who) == 80
        refused = await b.reserve(who, 40)
        assert isinstance(refused, Refusal) and await b.spent(who) == 80
        assert isinstance(await b.reserve(who, 20), Reservation) and await b.spent(who) == 100

    async def test_the_first_refusal_of_the_day_says_so_once(self, redis: Redis) -> None:
        b, who = budget(redis, 10), org()
        firsts = [(await b.reserve(who, 50)).exhausted_first for _ in range(3)]  # type: ignore[union-attr]
        assert firsts == [True, False, False]

    async def test_the_80_percent_line_is_crossed_once(self, redis: Redis) -> None:
        b, who = budget(redis, 100), org()
        results = [await b.reserve(who, 30) for _ in range(3)]  # 30, 60, 90
        assert [r.warned_first for r in results if isinstance(r, Reservation)] == [
            False,
            False,
            True,
        ]
        later = await b.reserve(who, 5)
        assert isinstance(later, Reservation) and later.warned_first is False

    async def test_parallel_calls_cannot_pass_the_cap_together(self, redis: Redis) -> None:
        b, who = budget(redis, 100), org()
        outcomes = await asyncio.gather(*(b.reserve(who, 10) for _ in range(60)))
        assert sum(isinstance(o, Reservation) for o in outcomes) == 10
        assert await b.spent(who) == 100

    async def test_a_cap_of_zero_allows_nothing(self, redis: Redis) -> None:
        assert isinstance(await budget(redis, 0).reserve(org(), 1), Refusal)

    async def test_the_organizations_have_counters_of_their_own(self, redis: Redis) -> None:
        b, one, two = budget(redis, 50), org(), org()
        assert isinstance(await b.reserve(one, 50), Reservation)
        assert isinstance(await b.reserve(one, 1), Refusal)
        assert isinstance(await b.reserve(two, 50), Reservation)

    async def test_a_new_day_starts_a_new_counter(self, redis: Redis) -> None:
        clock = FixedClock()
        b, who = budget(redis, 100, clock), org()
        assert isinstance(await b.reserve(who, 90), Reservation)
        clock.current += timedelta(days=1)
        assert await b.spent(who) == 0
        later = await b.reserve(who, 85)
        assert isinstance(later, Reservation) and later.warned_first  # a new day warns again

    async def test_the_keys_are_in_the_workers_namespace(self, redis: Redis) -> None:
        b, who = budget(redis, 100), org()
        reservation = await b.reserve(who, 10)
        assert isinstance(reservation, Reservation) and reservation.key.startswith(KEY_PREFIX)
        assert KEY_PREFIX == "pharos:worker:llm:"
        assert await redis.ttl(reservation.key) > 0  # it does not live for ever


class TestSettle:
    async def test_the_estimate_is_replaced_by_the_real_cost(self, redis: Redis) -> None:
        b, who = budget(redis, 1000), org()
        reservation = await b.reserve(who, 400)
        assert isinstance(reservation, Reservation)
        await b.settle(reservation, 150)
        assert await b.spent(who) == 150
        second = await b.reserve(who, 100)
        assert isinstance(second, Reservation)
        await b.settle(second, 130)  # a call can cost more than its estimate
        assert await b.spent(who) == 280

    async def test_a_released_booking_leaves_nothing_and_the_counter_never_goes_below_zero(
        self, redis: Redis
    ) -> None:
        b, who = budget(redis, 1000), org()
        reservation = await b.reserve(who, 400)
        assert isinstance(reservation, Reservation)
        await b.release(reservation)
        assert await b.spent(who) == 0
        await b.release(reservation)  # a second release by mistake
        assert await b.spent(who) == 0


class TestPace:
    async def test_it_counts_calls_in_a_minute(self, redis: Redis) -> None:
        clock = FixedClock()
        b, who = budget(redis, 100, clock), org()
        assert [await b.pace(who, 3) for _ in range(5)] == [True, True, True, False, False]
        clock.current += timedelta(minutes=1)
        assert await b.pace(who, 3) is True


class TestFailClosed:
    async def test_when_redis_does_not_answer_nothing_is_allowed(self) -> None:
        dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.5)
        b = budget(dead, 100)
        with pytest.raises(BudgetUnavailable):
            await b.reserve(org(), 1)
        with pytest.raises(BudgetUnavailable):
            await b.pace(org(), 5)
        with pytest.raises(BudgetUnavailable):
            await b.spent(org())
        await dead.aclose()


class TestTheUsageRecord:
    async def test_the_worker_writes_numbers_and_the_application_reads_them(
        self,
        worker_engine: AsyncEngine,
        app_engine: AsyncEngine,
        orgs: Orgs,
    ) -> None:
        mine, other = orgs
        sink = DatabaseUsageSink(worker_engine)
        await sink.record(UsageRecord(mine, "enrichment", "anthropic", "m", 120, 30, 5000, "ok"))
        await sink.record(UsageRecord(other, "insight", "anthropic", "m", 1, 1, 7, "refused"))

        async def seen_by_the_application(org_id: uuid.UUID) -> list[tuple[str, int]]:
            async with app_engine.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)}
                )
                rows = await connection.execute(
                    text("SELECT stage, cost_micro_usd FROM llm_usage ORDER BY id")
                )
                return [(r.stage, r.cost_micro_usd) for r in rows]

        assert await seen_by_the_application(mine) == [("enrichment", 5000)]
        assert await seen_by_the_application(other) == [("insight", 7)]

    async def test_a_record_the_database_does_not_accept_is_refused(
        self,
        worker_engine: AsyncEngine,
        orgs: Orgs,
    ) -> None:
        sink = DatabaseUsageSink(worker_engine)
        for bad in (
            UsageRecord(orgs[0], "enrichment", "p", "m", 1, 1, 1, "weird"),
            UsageRecord(orgs[0], "other-stage", "p", "m", 1, 1, 1, "ok"),
            UsageRecord(orgs[0], "enrichment", "p", "m", -1, 1, 1, "ok"),
            UsageRecord(orgs[0], "enrichment", "p" * 41, "m", 1, 1, 1, "ok"),
        ):
            with pytest.raises(DBAPIError):
                await sink.record(bad)

    async def test_a_record_for_another_organization_than_the_one_entered_is_refused(
        self,
        worker_engine: AsyncEngine,
        orgs: Orgs,
    ) -> None:
        async with worker_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.org_id', :o, true)"), {"o": str(orgs[0])}
            )
            with pytest.raises(DBAPIError, match="row-level security"):
                await connection.execute(
                    text(
                        "INSERT INTO llm_usage (org_id, stage, provider, model, tokens_in, "
                        "tokens_out, cost_micro_usd, outcome) VALUES (:o, 'filter', 'p', 'm', "
                        "0, 0, 0, 'ok')"
                    ),
                    {"o": orgs[1]},
                )


class RecordingBudget(Budget):
    """Remembers the estimate of each booking, so a test can size a cap from the real thing."""

    estimates: list[int]

    async def reserve(self, org: uuid.UUID, estimate: int):  # type: ignore[no-untyped-def]
        self.estimates = [*getattr(self, "estimates", []), estimate]
        return await super().reserve(org, estimate)


class TestTheWholeThing:
    async def test_parallel_calls_never_spend_more_than_the_cap(
        self, redis: Redis, worker_engine: AsyncEngine, owner_engine: AsyncEngine, orgs: Orgs
    ) -> None:
        who = orgs[0]
        clock = FixedClock()
        config = GatewayConfig(models={s: "m" for s in Stage}, calls_per_minute=10_000)
        probe_budget = RecordingBudget(redis, clock, daily_cap_micro_usd=10**12)
        sizing = LLMGateway(
            FakeProvider(GOOD), probe_budget, DatabaseUsageSink(worker_engine), config, clock
        )
        await sizing.generate(task=Task.ENRICH, org_id=orgs[1], article=article(), schema=Answer)
        [estimate] = probe_budget.estimates

        async def slow(model: str, system: str, user: str) -> ProviderAnswer:
            await asyncio.sleep(0.5)  # the bookings are held while the calls are in flight
            return ProviderAnswer(GOOD, 20, 10)

        gateway = LLMGateway(
            FakeProvider(script=slow),
            budget(redis, estimate * 12, clock),
            DatabaseUsageSink(worker_engine),
            config,
            clock,
        )

        async def one() -> str:
            try:
                await gateway.generate(
                    task=Task.ENRICH, org_id=who, article=article(), schema=Answer,
                )  # fmt: skip
            except BudgetExhausted:
                return "refused"
            return "ok"

        outcomes = await asyncio.gather(*(one() for _ in range(80)))
        assert outcomes.count("ok") == 12 and outcomes.count("refused") == 68
        spent = await budget(redis, estimate * 12, clock).spent(who)
        assert spent == 12 * (20 * 20 + 10 * 100)  # what was really used, at the default price
        async with owner_engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT outcome, count(*) AS n FROM llm_usage WHERE org_id = :o GROUP BY 1"
                    ),
                    {"o": who},
                )
            ).all()
        assert {r.outcome: r.n for r in rows} == {"ok": 12, "refused": 68}
