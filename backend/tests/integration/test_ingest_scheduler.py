"""Dispatcher, fetcher and cleanup against a real database, as the worker role (M3-07).

The worker runs under row-level security with column-level grants, so these tests also prove that
the SQL of the scheduler stays inside what the worker may do. Time is a clock the test moves.
"""

import asyncio
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pharos.ingest import backoff, repository
from pharos.ingest.connectors.base import ConfigError, ConnectorError, FetchOutcome, FetchState
from pharos.ingest.dispatcher import IN_FLIGHT_CAP, LEASE_SECONDS, PER_TICK, dispatch
from pharos.ingest.feed import RawEntry
from pharos.ingest.fetcher import fetch_source

from .conftest import TestDatabase
from .ingestion_support import Org, add_item, add_source, sha

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


def entry(n: int, title: str | None = None) -> RawEntry:
    url = f"https://news.example/s/{n}"
    return RawEntry(f"g{n}", title or f"Story {n}", "summary", url, T0, sha(f"{n}:{title}"))


class FakeConnector:
    type = "rss"

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[tuple[dict, FetchState]] = []
        self.during: list[Any] = []  # callbacks run in the middle of a fetch
        self.host = "news.example"

    def validate_config(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if "url" not in raw:
            raise ConfigError("url_required")
        return dict(raw)

    def dedupe_key(self, config: Mapping[str, Any]) -> str:
        return sha(config["url"])

    def politeness_host(self, config: Mapping[str, Any]) -> str | None:
        return self.host

    async def fetch(self, config, state, http, now):  # type: ignore[no-untyped-def]
        self.calls.append((dict(config), state))
        for callback in self.during:
            await callback()
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class OpenGate:
    gap_seconds = 5

    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.hosts: list[str] = []

    async def enter(self, host: str) -> bool:
        self.hosts.append(host)
        return self.allow


def ok(*entries: RawEntry, etag: str | None = '"v1"', **extra: Any) -> FetchOutcome:
    return FetchOutcome(
        "ok", entries=entries, etag=etag, bytes_fetched=100, entries_seen=len(entries), **extra
    )


async def make_source(owner_engine: AsyncEngine, org: Org, key: str, **columns: Any) -> uuid.UUID:
    columns.setdefault("enabled", True)
    columns.setdefault("config", '{"url": "https://news.example/feed"}')
    async with owner_engine.begin() as connection:
        return await add_source(connection, org, key=key, **columns)


async def row(owner_engine: AsyncEngine, source: uuid.UUID) -> Any:
    async with owner_engine.connect() as connection:
        return (
            await connection.execute(text("SELECT * FROM sources WHERE id = :i"), {"i": source})
        ).one()


async def table(owner_engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with owner_engine.connect() as connection:
        return list((await connection.execute(text(sql), params)).all())


def collect() -> tuple[list[tuple[uuid.UUID, uuid.UUID]], Any]:
    sent: list[tuple[uuid.UUID, uuid.UUID]] = []
    return sent, lambda source, token: sent.append((source, token))


# --- the dispatcher ----------------------------------------------------------------------------------


async def test_only_enabled_due_unleased_sources_are_leased_oldest_first(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    due_old = await make_source(owner_engine, alpha, "old", next_fetch_at=T0 - timedelta(hours=2))
    due_new = await make_source(owner_engine, alpha, "new", next_fetch_at=T0 - timedelta(minutes=1))
    await make_source(
        owner_engine, alpha, "off", enabled=False, next_fetch_at=T0 - timedelta(hours=1)
    )
    await make_source(owner_engine, alpha, "later", next_fetch_at=T0 + timedelta(minutes=1))
    await make_source(
        owner_engine, alpha, "busy", next_fetch_at=T0 - timedelta(hours=1),
        lease_token=uuid.uuid4(), lease_until=T0 + timedelta(minutes=2),
    )  # fmt: skip
    expired = await make_source(
        owner_engine, alpha, "expired", next_fetch_at=T0 - timedelta(hours=1),
        lease_token=uuid.uuid4(), lease_until=T0 - timedelta(seconds=1),
    )  # fmt: skip
    mine = await make_source(
        owner_engine, alpha, "mine", owner=alpha.user, next_fetch_at=T0 - timedelta(hours=3)
    )
    sent, enqueue = collect()
    report = await dispatch(worker_engine, slug=slug, now=T0, enqueue=enqueue)
    # The order of the hand-over is not promised; which sources are chosen is (next test).
    assert {s for s, _ in sent} == {mine, due_old, expired, due_new}  # a personal feed too
    assert len(sent) == 4
    assert (report.leased, report.enqueued, report.failed_to_enqueue) == (4, 4, 0)


async def test_personal_feeds_are_left_alone_when_the_operator_switched_them_off(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    shared = await make_source(owner_engine, alpha, "shared", next_fetch_at=T0 - timedelta(hours=1))
    mine = await make_source(
        owner_engine, alpha, "mine", owner=alpha.user, next_fetch_at=T0 - timedelta(hours=2)
    )
    sent, enqueue = collect()
    report = await dispatch(worker_engine, slug=slug, now=T0, enqueue=enqueue, personal=False)
    assert {s for s, _ in sent} == {shared}
    assert report.leased == 1
    assert (await row(owner_engine, mine)).lease_token is None  # never leased, so due again later
    sent, enqueue = collect()
    await dispatch(worker_engine, slug=slug, now=T0, enqueue=enqueue, personal=True)
    assert {s for s, _ in sent} == {mine}


async def test_when_there_is_not_room_for_everything_the_longest_waiting_go_first(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    # Created newest first, so the physical order of the rows is the opposite of the right answer.
    await make_source(owner_engine, alpha, "c", next_fetch_at=T0 - timedelta(hours=1))
    second = await make_source(owner_engine, alpha, "b", next_fetch_at=T0 - timedelta(hours=5))
    oldest = await make_source(owner_engine, alpha, "a", next_fetch_at=T0 - timedelta(hours=9))
    sent, enqueue = collect()
    await dispatch(worker_engine, slug=slug, now=T0, enqueue=enqueue, per_tick=2)
    assert {s for s, _ in sent} == {oldest, second}


async def test_the_lease_is_five_minutes_and_the_token_is_what_travels(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a", next_fetch_at=T0)
    sent, enqueue = collect()
    await dispatch(worker_engine, slug=slug, now=T0, enqueue=enqueue)
    stored = await row(owner_engine, source)
    assert stored.lease_until == T0 + timedelta(seconds=LEASE_SECONDS) == T0 + timedelta(minutes=5)
    assert sent == [(source, stored.lease_token)] and stored.lease_token is not None


async def test_the_lease_is_committed_before_the_task_is_enqueued(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha: Org,
    slug: str,
    database: TestDatabase,
) -> None:
    source = await make_source(owner_engine, alpha, "a", next_fetch_at=T0)
    seen: list[Any] = []

    def check_from_another_connection(source_id: uuid.UUID, token: uuid.UUID) -> None:
        async def look() -> None:
            engine = create_async_engine(database.owner_url)
            try:
                async with engine.connect() as connection:
                    found = await connection.execute(
                        text("SELECT lease_token FROM sources WHERE id = :i"), {"i": source_id}
                    )
                    seen.append(found.scalar_one() == token)
            finally:
                await engine.dispose()

        thread = threading.Thread(target=lambda: asyncio.run(look()))
        thread.start()
        thread.join()

    await dispatch(worker_engine, slug=slug, now=T0, enqueue=check_from_another_connection)
    assert seen == [True]
    assert (await row(owner_engine, source)).lease_token is not None


async def test_a_busy_broker_frees_the_source_for_the_next_run(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    first = await make_source(owner_engine, alpha, "a", next_fetch_at=T0 - timedelta(minutes=3))
    second = await make_source(owner_engine, alpha, "b", next_fetch_at=T0 - timedelta(minutes=2))
    sent: list[uuid.UUID] = []

    def flaky(source: uuid.UUID, token: uuid.UUID) -> None:
        if source == first:
            raise ConnectionError("broker down")
        sent.append(source)

    report = await dispatch(worker_engine, slug=slug, now=T0, enqueue=flaky)
    assert (report.leased, report.enqueued, report.failed_to_enqueue) == (2, 1, 1)
    assert sent == [second]
    freed = await row(owner_engine, first)
    assert (freed.lease_token, freed.lease_until) == (None, None)
    assert (await row(owner_engine, second)).lease_token is not None
    again, enqueue = collect()  # the freed source is picked up by the very next run
    await dispatch(worker_engine, slug=slug, now=T0 + timedelta(minutes=1), enqueue=enqueue)
    assert [s for s, _ in again] == [first]


async def test_at_most_a_batch_is_started_per_run_and_the_total_in_flight_is_capped(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    async with owner_engine.begin() as connection:
        for n in range(60):
            await add_source(
                connection, alpha, key=f"s{n}", enabled=True,
                config='{"url": "https://news.example/f"}',
                next_fetch_at=T0 - timedelta(minutes=n + 1),
            )  # fmt: skip
    sent, enqueue = collect()
    for minute in range(5):
        await dispatch(
            worker_engine, slug=slug, now=T0 + timedelta(seconds=minute), enqueue=enqueue
        )
    # 20 per run, but never more than 40 leased at once: the third run finds the room used up.
    assert len(sent) == IN_FLIGHT_CAP == 40 and PER_TICK == 20
    assert len({s for s, _ in sent}) == 40
    later, enqueue_later = collect()
    await dispatch(worker_engine, slug=slug, now=T0 + timedelta(minutes=10), enqueue=enqueue_later)
    assert len(later) == 20  # leases expired, the next batch of 20 starts


async def test_two_dispatchers_at_once_never_lease_the_same_source(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    async with owner_engine.begin() as connection:
        for n in range(30):
            await add_source(
                connection, alpha, key=f"s{n}", enabled=True,
                config='{"url": "https://news.example/f"}',
                next_fetch_at=T0 - timedelta(minutes=n + 1),
            )  # fmt: skip
    sent_a, enqueue_a = collect()
    sent_b, enqueue_b = collect()
    await asyncio.gather(
        dispatch(
            worker_engine, slug=slug, now=T0, enqueue=enqueue_a, per_tick=20, in_flight_cap=100
        ),
        dispatch(
            worker_engine, slug=slug, now=T0, enqueue=enqueue_b, per_tick=20, in_flight_cap=100
        ),
    )
    ids = [s for s, _ in sent_a + sent_b]
    assert len(ids) == len(set(ids)) == 30  # 20 + 10, no source twice, none missed
    assert len({t for _, t in sent_a + sent_b}) == 30


async def test_an_unknown_organization_does_nothing(worker_engine: AsyncEngine) -> None:
    sent, enqueue = collect()
    report = await dispatch(worker_engine, slug="nobody-here", now=T0, enqueue=enqueue)
    assert (report.leased, sent) == (0, [])


async def test_only_the_organization_of_the_installation_is_served(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org], slug: str
) -> None:
    await make_source(owner_engine, alpha_beta[1], "beta", next_fetch_at=T0 - timedelta(hours=1))
    sent, enqueue = collect()
    await dispatch(worker_engine, slug=slug, now=T0, enqueue=enqueue)
    assert sent == []


# --- the fetcher -------------------------------------------------------------------------------------------


async def leased(
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    org: Org,
    slug: str,
    clock: Clock,
    key: str = "a",
    **columns: Any,
) -> tuple[uuid.UUID, uuid.UUID]:
    await make_source(
        owner_engine, org, key, next_fetch_at=clock() - timedelta(minutes=1), **columns
    )
    sent, enqueue = collect()
    await dispatch(worker_engine, slug=slug, now=clock(), enqueue=enqueue)
    return sent[0]


async def run(
    worker_engine: AsyncEngine,
    slug: str,
    source: uuid.UUID,
    token: uuid.UUID,
    clock: Clock,
    connector: FakeConnector,
    gate: OpenGate | None = None,
):  # type: ignore[no-untyped-def]
    return await fetch_source(
        worker_engine, slug=slug, source_id=source, lease_token=token, clock=clock,
        connectors={"rss": connector}, http=None,  # type: ignore[arg-type]
        gate=gate or OpenGate(),  # type: ignore[arg-type]
    )  # fmt: skip


async def test_a_fetch_stores_the_entries_and_schedules_the_next_one(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(
        worker_engine, owner_engine, alpha, slug, clock, interval_seconds=3600
    )
    connector = FakeConnector(ok(entry(1), entry(2), etag='"v1"', last_modified="Tue, 27 Oct 2026"))
    clock.advance(seconds=3)
    report = await run(worker_engine, slug, source, token, clock, connector)
    assert (report.result, report.items_new) == ("ok", 2)
    stored = await row(owner_engine, source)
    assert (stored.lease_token, stored.lease_until) == (None, None)
    assert (stored.etag, stored.last_modified) == ('"v1"', "Tue, 27 Oct 2026")
    assert (stored.consecutive_failures, stored.last_error_code) == (0, None)
    assert stored.last_success_at == clock() and stored.last_fetched_at is not None
    assert stored.next_fetch_at == backoff.after_success(clock(), 3600, source)
    runs = await table(owner_engine, "SELECT * FROM fetch_runs WHERE source_id = :s", s=source)
    assert len(runs) == 1
    assert (runs[0].outcome, runs[0].items_seen, runs[0].items_new, runs[0].bytes_fetched) == (
        "ok",
        2,
        2,
        100,
    )
    assert runs[0].finished_at == clock() and runs[0].duration_ms == 0
    raw = await table(
        owner_engine,
        "SELECT external_id, title FROM raw_items WHERE source_id = :s ORDER BY 1",
        s=source,
    )
    assert [tuple(r) for r in raw] == [("g1", "Story 1"), ("g2", "Story 2")]


async def test_the_next_fetch_asks_with_the_validators_from_the_last_one(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(worker_engine, owner_engine, alpha, slug, clock)
    connector = FakeConnector(
        ok(entry(1), etag='"v1"', last_modified="d1"),
        FetchOutcome("not_modified", etag='"v1"', last_modified="d1"),
    )
    await run(worker_engine, slug, source, token, clock, connector)
    clock.advance(hours=2)
    sent, enqueue = collect()
    await dispatch(worker_engine, slug=slug, now=clock(), enqueue=enqueue)
    second = await run(worker_engine, slug, *sent[0], clock, connector)
    assert second.result == "not_modified"
    assert connector.calls[0][1] == FetchState(None, None)
    assert connector.calls[1][1] == FetchState('"v1"', "d1")
    runs = await table(owner_engine, "SELECT outcome, items_new FROM fetch_runs ORDER BY id")
    assert [tuple(r) for r in runs] == [("ok", 1), ("not_modified", 0)]
    assert len(await table(owner_engine, "SELECT 1 FROM raw_items")) == 1  # nothing added by a 304


async def test_fetching_the_same_feed_again_adds_nothing_and_a_changed_entry_replaces_its_copy(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(worker_engine, owner_engine, alpha, slug, clock)
    connector = FakeConnector(
        ok(entry(1), entry(2)), ok(entry(1), entry(2)), ok(entry(1), entry(2, "Story two, edited"))
    )
    assert (await run(worker_engine, slug, source, token, clock, connector)).items_new == 2
    for expected in (0, 0):  # the edit refreshes an item; it creates none
        clock.advance(hours=2)
        sent, enqueue = collect()
        await dispatch(worker_engine, slug=slug, now=clock(), enqueue=enqueue)
        assert (await run(worker_engine, slug, *sent[0], clock, connector)).items_new == expected
    raw = await table(owner_engine, "SELECT external_id, title FROM raw_items ORDER BY 1")
    assert [tuple(r) for r in raw] == [("g1", "Story 1"), ("g2", "Story two, edited")]
    stored = await table(owner_engine, "SELECT title FROM items ORDER BY canonical_url")
    assert [r.title for r in stored] == ["Story 1", "Story two, edited"]


async def test_a_task_delivered_twice_does_the_work_once(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(worker_engine, owner_engine, alpha, slug, clock)
    connector = FakeConnector(ok(entry(1)), ok(entry(1)))
    results = await asyncio.gather(
        run(worker_engine, slug, source, token, clock, connector),
        run(worker_engine, slug, source, token, clock, connector),
    )
    assert sorted(r.result for r in results) == ["ok", "stale"]
    assert len(connector.calls) == 1  # one fetch, not two: the second copy found the old token gone
    again = await run(worker_engine, slug, source, token, clock, connector)
    assert again.result == "stale"
    assert len(await table(owner_engine, "SELECT 1 FROM raw_items")) == 1


@pytest.mark.parametrize("what", ["wrong token", "expired", "disabled", "unknown source"])
async def test_a_task_whose_lease_is_not_live_does_nothing(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str, what: str
) -> None:
    clock = Clock()
    source, token = await leased(worker_engine, owner_engine, alpha, slug, clock)
    if what == "wrong token":
        token = uuid.uuid4()
    elif what == "expired":
        clock.advance(seconds=LEASE_SECONDS + 1)
    elif what == "disabled":
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("UPDATE sources SET enabled = false WHERE id = :i"), {"i": source}
            )
    else:
        source = uuid.uuid4()
    connector = FakeConnector(ok(entry(1)))
    report = await run(worker_engine, slug, source, token, clock, connector)
    assert report.result == "stale" and connector.calls == []
    assert await table(owner_engine, "SELECT 1 FROM fetch_runs") == []
    assert await table(owner_engine, "SELECT 1 FROM raw_items") == []


async def test_a_source_of_another_organization_cannot_be_fetched(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org], slug: str
) -> None:
    clock = Clock()
    foreign = await make_source(
        owner_engine,
        alpha_beta[1],
        "beta",
        lease_token=uuid.uuid4(),
        lease_until=T0 + timedelta(minutes=5),
    )
    token = (await row(owner_engine, foreign)).lease_token
    connector = FakeConnector(ok(entry(1)))
    assert (await run(worker_engine, slug, foreign, token, clock, connector)).result == "stale"
    assert connector.calls == []


async def test_failures_back_off_and_obey_retry_after_and_a_success_resets_everything(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(
        worker_engine, owner_engine, alpha, slug, clock, interval_seconds=900
    )
    connector = FakeConnector(
        ConnectorError("bad_status", "http_error", http_status=503, retry_after=7200),
        ConnectorError("timeout", "timed_out"),
        ok(entry(1)),
    )
    first = await run(worker_engine, slug, source, token, clock, connector)
    assert (first.result, first.error_code) == ("failed", "http_error")
    stored = await row(owner_engine, source)
    assert (stored.consecutive_failures, stored.last_error_code) == (1, "http_error")
    assert stored.next_fetch_at >= clock() + timedelta(
        seconds=7200
    )  # the server asked for two hours
    assert (stored.lease_token, stored.last_success_at) == (None, None)
    run1 = (await table(owner_engine, "SELECT * FROM fetch_runs ORDER BY id"))[0]
    assert (run1.outcome, run1.error_code, run1.http_status) == ("bad_status", "http_error", 503)

    clock.current = stored.next_fetch_at + timedelta(seconds=1)
    sent, enqueue = collect()
    await dispatch(worker_engine, slug=slug, now=clock(), enqueue=enqueue)
    await run(worker_engine, slug, *sent[0], clock, connector)
    stored = await row(owner_engine, source)
    assert stored.consecutive_failures == 2
    assert stored.next_fetch_at - clock() >= timedelta(seconds=1800)  # 900 * 2 ** 1

    clock.current = stored.next_fetch_at + timedelta(seconds=1)
    sent, enqueue = collect()
    await dispatch(worker_engine, slug=slug, now=clock(), enqueue=enqueue)
    await run(worker_engine, slug, *sent[0], clock, connector)
    stored = await row(owner_engine, source)
    assert (stored.consecutive_failures, stored.last_error_code) == (0, None)
    assert stored.next_fetch_at == backoff.after_success(clock(), 900, source)


async def test_after_ten_failures_in_a_row_a_source_is_tried_once_a_day(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(
        worker_engine,
        owner_engine,
        alpha,
        slug,
        clock,
        interval_seconds=300,
        consecutive_failures=9,
    )
    connector = FakeConnector(ConnectorError("network", "unreachable"))
    await run(worker_engine, slug, source, token, clock, connector)
    stored = await row(owner_engine, source)
    assert stored.consecutive_failures == 10
    assert stored.next_fetch_at - clock() >= backoff.MAX_BACKOFF
    assert stored.enabled is True  # only a person disables a source


async def test_a_connector_that_crashes_is_a_failed_run_not_a_dead_worker(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(worker_engine, owner_engine, alpha, slug, clock)
    report = await run(
        worker_engine,
        slug,
        source,
        token,
        clock,
        FakeConnector(RuntimeError("secret detail http://x.example")),
    )
    assert (report.result, report.error_code) == ("failed", "unexpected")
    stored = await row(owner_engine, source)
    assert stored.last_error_code == "unexpected" and stored.consecutive_failures == 1
    assert "secret" not in str(await table(owner_engine, "SELECT * FROM fetch_runs"))


async def test_an_unsupported_type_and_a_broken_configuration_are_failed_runs(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(
        worker_engine, owner_engine, alpha, slug, clock, key="gh", type="github"
    )
    report = await run(worker_engine, slug, source, token, clock, FakeConnector())
    assert report.error_code == "unsupported"
    broken, broken_token = await leased(
        worker_engine, owner_engine, alpha, slug, clock, key="bad", config="{}"
    )
    connector = FakeConnector()
    report = await run(worker_engine, slug, broken, broken_token, clock, connector)
    assert (report.error_code, connector.calls) == ("bad_config", [])


async def test_nothing_is_held_open_in_the_database_while_the_network_is_used(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(worker_engine, owner_engine, alpha, slug, clock)
    connector = FakeConnector(ok(entry(1)))
    observed: list[list[Any]] = []

    async def look() -> None:
        observed.append(
            await table(
                owner_engine,
                "SELECT state FROM pg_stat_activity WHERE usename = 'pharos_worker' "
                "AND state LIKE 'idle in transaction%'",
            )
        )

    connector.during.append(look)
    await run(worker_engine, slug, source, token, clock, connector)
    assert observed == [[]]


async def test_a_lease_lost_during_the_fetch_discards_the_result(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(worker_engine, owner_engine, alpha, slug, clock)
    connector = FakeConnector(ok(entry(1), entry(2)))
    thief = uuid.uuid4()

    async def steal() -> None:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("UPDATE sources SET lease_token = :t WHERE id = :i"), {"t": thief, "i": source}
            )

    connector.during.append(steal)
    report = await run(worker_engine, slug, source, token, clock, connector)
    assert report.result == "stale"
    assert (await row(owner_engine, source)).lease_token == thief  # untouched by the late worker
    assert await table(owner_engine, "SELECT 1 FROM raw_items") == []  # rolled back
    assert [r.outcome for r in await table(owner_engine, "SELECT outcome FROM fetch_runs")] == [
        "running"
    ]


async def test_a_busy_host_defers_the_fetch_without_a_run(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(worker_engine, owner_engine, alpha, slug, clock)
    connector, gate = FakeConnector(ok(entry(1))), OpenGate(allow=False)
    report = await run(worker_engine, slug, source, token, clock, connector, gate)
    assert report.result == "deferred" and connector.calls == [] and gate.hosts == ["news.example"]
    stored = await row(owner_engine, source)
    assert stored.lease_token is None and stored.next_fetch_at > clock()
    assert stored.consecutive_failures == 0  # being polite is not a failure
    assert await table(owner_engine, "SELECT 1 FROM fetch_runs") == []


async def test_a_worker_that_dies_leaves_a_lease_that_runs_out_and_the_source_is_fetched_again(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    class Killed(BaseException):
        pass

    clock = Clock()
    source, token = await leased(worker_engine, owner_engine, alpha, slug, clock)
    with pytest.raises(Killed):
        await run(worker_engine, slug, source, token, clock, FakeConnector(Killed()))
    dead = await row(owner_engine, source)
    assert dead.lease_token is not None  # nobody released it
    sent, enqueue = collect()
    await dispatch(worker_engine, slug=slug, now=clock(), enqueue=enqueue)
    assert sent == []  # still leased: not fetched twice at once
    assert dead.lease_token != token  # starting the run replaced the token
    clock.advance(seconds=LEASE_SECONDS + 1)
    await dispatch(worker_engine, slug=slug, now=clock(), enqueue=enqueue)
    assert [s for s, _ in sent] == [source] and sent[0][1] != token
    report = await run(worker_engine, slug, *sent[0], clock, FakeConnector(ok(entry(1))))
    assert report.result == "ok"
    assert (
        await run(worker_engine, slug, source, token, clock, FakeConnector(ok(entry(1))))
    ).result == "stale"
    outcomes = sorted(
        r.outcome for r in await table(owner_engine, "SELECT outcome FROM fetch_runs")
    )
    assert outcomes == ["ok", "running"]


# --- cleanup ---------------------------------------------------------------------------------------------------


async def test_cleanup_deletes_exactly_what_is_past_its_retention(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a")
    now = T0
    async with owner_engine.begin() as connection:
        for days, label in ((30, "run-old"), (29, "run-new")):
            await connection.execute(
                text(
                    "INSERT INTO fetch_runs (org_id, source_id, started_at, outcome) VALUES (:o, :s, :t, 'ok')"
                ),
                {
                    "o": alpha.id,
                    "s": source,
                    "t": now - timedelta(days=days, minutes=1 if label == "run-old" else -1),
                },
            )
        for hours, ext in ((14 * 24 + 1, "raw-old"), (14 * 24 - 1, "raw-new")):
            await connection.execute(
                text(
                    "INSERT INTO raw_items (org_id, source_id, external_id, content_sha256, fetched_at) VALUES (:o, :s, :e, :h, :t)"
                ),
                {
                    "o": alpha.id,
                    "s": source,
                    "e": ext,
                    "h": sha(ext),
                    "t": now - timedelta(hours=hours),
                },
            )
        old, fresh = now - timedelta(days=180, minutes=1), now - timedelta(days=179)
        await add_item(connection, alpha, url="https://x.example/old-new", fetched_at=old)
        await add_item(connection, alpha, url="https://x.example/fresh", fetched_at=fresh)
        await add_item(
            connection,
            alpha,
            url="https://x.example/old-held",
            fetched_at=old,
            status="held",
            hold_reason="manual",
        )
        await add_item(
            connection, alpha, url="https://x.example/old-hidden", fetched_at=old, status="hidden"
        )
        await add_item(
            connection, alpha, url="https://x.example/old-mine", fetched_at=old, owner=alpha.user
        )

    async def clean() -> dict[str, int]:
        async with worker_engine.begin() as connection:
            await repository.enter_org(connection, alpha.id)
            return await repository.cleanup(connection, now, repository.Retention())

    counts = await clean()
    assert counts == {"abandoned_runs": 0, "fetch_runs": 1, "raw_items": 1, "items": 2}
    left = {r.canonical_url for r in await table(owner_engine, "SELECT canonical_url FROM items")}
    assert left == {
        "https://x.example/fresh",
        "https://x.example/old-held",
        "https://x.example/old-hidden",
    }
    assert (await clean()) == {"abandoned_runs": 0, "fetch_runs": 0, "raw_items": 0, "items": 0}


async def test_a_run_that_never_finished_is_marked_abandoned_after_an_hour(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org
) -> None:
    source = await make_source(owner_engine, alpha, "a")
    async with owner_engine.begin() as connection:
        for minutes in (59, 61):
            await connection.execute(
                text("INSERT INTO fetch_runs (org_id, source_id, started_at) VALUES (:o, :s, :t)"),
                {"o": alpha.id, "s": source, "t": T0 - timedelta(minutes=minutes)},
            )
    async with worker_engine.begin() as connection:
        await repository.enter_org(connection, alpha.id)
        counts = await repository.cleanup(connection, T0, repository.Retention())
    assert counts["abandoned_runs"] == 1
    runs = await table(
        owner_engine, "SELECT outcome, error_code, finished_at FROM fetch_runs ORDER BY started_at"
    )
    assert [(r.outcome, r.error_code) for r in runs] == [("error", "abandoned"), ("running", None)]
    assert runs[0].finished_at == T0


async def test_a_failure_after_the_lease_was_lost_changes_nothing(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(worker_engine, owner_engine, alpha, slug, clock)
    connector = FakeConnector(ConnectorError("network", "unreachable"))
    thief = uuid.uuid4()

    async def steal() -> None:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("UPDATE sources SET lease_token = :t WHERE id = :i"), {"t": thief, "i": source}
            )

    connector.during.append(steal)
    report = await run(worker_engine, slug, source, token, clock, connector)
    assert report.result == "stale"
    stored = await row(owner_engine, source)
    assert (stored.lease_token, stored.consecutive_failures, stored.last_error_code) == (
        thief,
        0,
        None,
    )
    assert [r.outcome for r in await table(owner_engine, "SELECT outcome FROM fetch_runs")] == [
        "running"
    ]


async def test_a_task_that_starts_too_late_to_finish_inside_its_lease_is_refused(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(worker_engine, owner_engine, alpha, slug, clock)
    clock.advance(seconds=LEASE_SECONDS - 159)  # 159 s left: less than a fetch may take
    connector = FakeConnector(ok(entry(1)))
    assert (await run(worker_engine, slug, source, token, clock, connector)).result == "stale"
    assert connector.calls == [] and await table(owner_engine, "SELECT 1 FROM fetch_runs") == []
    # With 161 s left it goes ahead (positive control).
    other, other_token = await leased(worker_engine, owner_engine, alpha, slug, clock, key="b")
    clock.advance(seconds=LEASE_SECONDS - 161)
    fresh = FakeConnector(ok(entry(2)))
    assert (await run(worker_engine, slug, other, other_token, clock, fresh)).result == "ok"


async def test_a_result_the_database_refuses_is_a_failure_with_backoff_not_an_endless_retry(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    clock = Clock()
    source, token = await leased(
        worker_engine, owner_engine, alpha, slug, clock, interval_seconds=900
    )
    bad = RawEntry("g1", "T", "s", "https://news.example/a\x85b", T0, sha("bad"))  # fails a CHECK
    report = await run(worker_engine, slug, source, token, clock, FakeConnector(ok(bad)))
    assert (report.result, report.error_code) == ("failed", "store_failed")
    stored = await row(owner_engine, source)
    assert (stored.consecutive_failures, stored.last_error_code) == (1, "store_failed")
    assert stored.lease_token is None and stored.next_fetch_at >= clock() + timedelta(seconds=900)
    runs = await table(owner_engine, "SELECT outcome, error_code, finished_at FROM fetch_runs")
    assert [(r.outcome, r.error_code) for r in runs] == [("error", "store_failed")]
    assert runs[0].finished_at is not None
    assert await table(owner_engine, "SELECT 1 FROM raw_items") == []  # nothing half-stored


async def test_an_unreachable_host_gate_defers_instead_of_fetching(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    class BrokenGate(OpenGate):
        async def enter(self, host: str) -> bool:
            raise ConnectionError("redis is down")

    clock = Clock()
    source, token = await leased(worker_engine, owner_engine, alpha, slug, clock)
    connector = FakeConnector(ok(entry(1)))
    report = await run(worker_engine, slug, source, token, clock, connector, BrokenGate())
    assert report.result == "deferred" and connector.calls == []
    stored = await row(owner_engine, source)
    assert stored.lease_token is None and stored.next_fetch_at > clock()


async def test_organization_feeds_are_leased_before_personal_ones_however_old(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    for n in range(3):
        await make_source(
            owner_engine, alpha, f"mine{n}", owner=alpha.user, next_fetch_at=T0 - timedelta(days=5)
        )
    shared = [
        await make_source(
            owner_engine, alpha, f"org{n}", next_fetch_at=T0 - timedelta(minutes=n + 1)
        )
        for n in range(3)
    ]
    sent, enqueue = collect()
    await dispatch(worker_engine, slug=slug, now=T0, enqueue=enqueue, per_tick=3)
    assert {s for s, _ in sent} == set(shared)
