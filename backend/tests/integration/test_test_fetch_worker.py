"""The worker's side of the test fetch, as the worker role against a real database (ADR-0013).

The trial fetch must be harmless to everything an ordinary fetch would change: it holds no lease and
writes nothing to the database. It reads only organization sources, re-checks the stored
configuration, and leaves one small answer in Redis.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.ingest import testfetch
from pharos.ingest.connectors.base import ConnectorError
from pharos.ingest.connectors.rss import RssConnector
from pharos.ingest.feed import RawEntry
from pharos.ingest.testrun import run_test_fetch

from .ingestion_support import Org, sha
from .test_ingest_scheduler import (
    Clock,
    FakeConnector,
    OpenGate,
    make_source,
    ok,
    table,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


class Store:
    def __init__(self) -> None:
        self.written: list[tuple[str, str, int]] = []

    async def set(self, name: str, value: str, *, ex: int) -> None:
        self.written.append((name, value, ex))


def entry(n: int, title: str | None = None) -> RawEntry:
    url = f"https://news.example/s/{n}"
    return RawEntry(f"g{n}", title or f"Story {n}", "summary", url, NOW, sha(f"{n}:{title}"))


async def trial(
    worker_engine: AsyncEngine,
    slug: str,
    source: uuid.UUID,
    connector: FakeConnector,
    *,
    gate: OpenGate | None = None,
    connectors: dict[str, Any] | None = None,
) -> tuple[testfetch.TestResult, Store, uuid.UUID]:
    test_id = uuid.uuid4()
    store = Store()
    result = await run_test_fetch(
        worker_engine,
        slug=slug,
        source_id=source,
        test_id=test_id,
        connectors=connectors if connectors is not None else {"rss": connector},
        http=None,  # type: ignore[arg-type]
        gate=gate or OpenGate(),  # type: ignore[arg-type]
        store=store,
        clock=Clock(),
    )
    return result, store, test_id


async def everything_about(owner_engine: AsyncEngine) -> dict[str, Any]:
    """What an ordinary fetch could change, as one comparable picture."""
    tables = ("fetch_runs", "raw_items", "items", "item_sources")
    counts = {t: len(await table(owner_engine, f"SELECT 1 FROM {t}")) for t in tables}
    sources = [tuple(r) for r in await table(owner_engine, "SELECT * FROM sources ORDER BY id")]
    return {"counts": counts, "sources": sources}


async def test_a_trial_fetch_answers_with_a_few_samples_and_stores_them_briefly(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a")
    connector = FakeConnector(ok(*[entry(n) for n in range(1, 9)]))
    result, store, test_id = await trial(worker_engine, slug, source, connector)
    assert (result.outcome, result.entries_seen, result.bytes_fetched) == ("ok", 8, 100)
    assert [s.title for s in result.sample] == [f"Story {n}" for n in range(1, 6)]
    [(key, value, ttl)] = store.written
    assert key == f"pharos:worker:testfetch:{test_id}" and ttl == testfetch.RESULT_TTL_SECONDS
    # exactly what the API's reader accepts, for this test of this source
    assert testfetch.parse_result(value, test_id=test_id, source_id=source) == result
    assert "summary" not in value  # no excerpt, no body: the samples are title, link and date


async def test_a_trial_fetch_writes_nothing_to_the_database(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a", etag='"old"', consecutive_failures=2)
    before = await everything_about(owner_engine)
    connector = FakeConnector(
        ok(entry(1), entry(2), etag='"new"', last_modified="Tue, 27 Oct 2026")
    )
    result, _, _ = await trial(worker_engine, slug, source, connector)
    assert result.outcome == "ok"
    assert await everything_about(owner_engine) == before
    assert before["counts"] == {"fetch_runs": 0, "raw_items": 0, "items": 0, "item_sources": 0}


async def test_a_trial_fetch_asks_without_the_validators_so_it_always_gets_a_body(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a", etag='"old"', last_modified="Tue")
    connector = FakeConnector(ok(entry(1)))
    await trial(worker_engine, slug, source, connector)
    [(config, state)] = connector.calls
    assert (state.etag, state.last_modified) == (None, None)
    assert config == {"url": "https://news.example/feed"}


async def test_a_source_that_is_not_enabled_yet_can_be_tried(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a", enabled=False)
    connector = FakeConnector(ok(entry(1)))
    result, _, _ = await trial(worker_engine, slug, source, connector)
    assert result.outcome == "ok" and len(connector.calls) == 1


async def test_the_politeness_gate_is_asked_for_the_host_of_the_source(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a")
    gate = OpenGate()
    await trial(worker_engine, slug, source, FakeConnector(ok(entry(1))), gate=gate)
    assert gate.hosts == ["news.example"]


async def test_a_busy_host_is_reported_and_not_fetched(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a")
    connector = FakeConnector(ok(entry(1)))
    result, store, _ = await trial(worker_engine, slug, source, connector, gate=OpenGate(False))
    assert result.outcome == "host_busy" and connector.calls == []
    assert len(store.written) == 1  # the person still gets an answer


async def test_a_personal_feed_is_never_fetched_on_an_administrators_request(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "mine", owner=alpha.user, interval_seconds=3600)
    connector = FakeConnector(ok(entry(1)))
    result, _, _ = await trial(worker_engine, slug, source, connector)
    assert result.outcome == "source_missing" and connector.calls == []


async def test_a_source_of_another_organization_or_no_source_at_all_is_missing(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org], slug: str
) -> None:
    theirs = await make_source(owner_engine, alpha_beta[1], "b")
    connector = FakeConnector(ok(entry(1)), ok(entry(1)))
    for source in (theirs, uuid.uuid4()):
        result, _, _ = await trial(worker_engine, slug, source, connector)
        assert result.outcome == "source_missing"
    assert connector.calls == []


async def test_an_unknown_organization_is_a_missing_source(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org
) -> None:
    source = await make_source(owner_engine, alpha, "a")
    connector = FakeConnector(ok(entry(1)))
    result, _, _ = await trial(worker_engine, "no-such-organization", source, connector)
    assert result.outcome == "source_missing" and connector.calls == []


@pytest.mark.parametrize(
    "config", ['{"url": "javascript:alert(1)"}', '{"url": "https://news.example/f", "x": 1}', "{}"]
)
async def test_a_stored_configuration_that_is_not_valid_is_not_used(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str, config: str
) -> None:
    source = await make_source(owner_engine, alpha, "a", config=config)
    # the real connector: its rules decide, and it would fail on `http=None` if it were asked to fetch
    result, _, _ = await trial(
        worker_engine, slug, source, FakeConnector(), connectors={"rss": RssConnector()}
    )
    assert (result.outcome, result.error_code) == ("error", "config_invalid")


async def test_a_type_without_a_connector_is_an_error_not_a_crash(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a")
    result, _, _ = await trial(worker_engine, slug, source, FakeConnector(), connectors={})
    assert (result.outcome, result.error_code) == ("error", "unsupported_type")


async def test_a_failed_fetch_is_reported_with_fixed_words_only(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a")
    failure = ConnectorError("bad_status", "http_error", http_status=404)
    result, store, test_id = await trial(worker_engine, slug, source, FakeConnector(failure))
    assert (result.outcome, result.error_code, result.http_status) == (
        "bad_status",
        "http_error",
        404,
    )
    assert result.sample == [] and result.entries_seen == 0
    assert testfetch.parse_result(store.written[0][1], test_id=test_id, source_id=source) == result


async def test_a_connector_that_crashes_is_an_error_not_a_dead_worker(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a")
    result, store, _ = await trial(worker_engine, slug, source, FakeConnector(RuntimeError("boom")))
    assert (result.outcome, result.error_code) == ("error", "unexpected")
    assert "boom" not in store.written[0][1]  # nothing the exception said reaches the answer


async def test_nothing_is_held_open_in_the_database_while_the_network_is_used(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a")
    connector = FakeConnector(ok(entry(1)))
    seen: list[list[Any]] = []

    async def look() -> None:
        seen.append(
            await table(
                owner_engine,
                "SELECT state FROM pg_stat_activity WHERE usename = 'pharos_worker' "
                "AND state LIKE 'idle in transaction%'",
            )
        )

    connector.during.append(look)
    await trial(worker_engine, slug, source, connector)
    assert seen == [[]]


async def test_an_article_with_a_title_the_reader_would_refuse_is_left_out_of_the_sample(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a")
    hostile = RawEntry("h", "<b>Big</b> news", "x", "https://news.example/h", None, sha("h"))
    ok_entries = [entry(n) for n in range(1, 8)]
    result, store, test_id = await trial(
        worker_engine, slug, source, FakeConnector(ok(hostile, *ok_entries))
    )
    # the answer is still a good one; the odd article is not in it, and five good ones are
    assert result.outcome == "ok" and result.entries_seen == 8
    assert [s.title for s in result.sample] == [f"Story {n}" for n in range(1, 6)]
    assert testfetch.parse_result(store.written[0][1], test_id=test_id, source_id=source) == result


async def test_a_feed_whose_every_title_is_odd_still_answers_ok_with_no_sample(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, slug: str
) -> None:
    source = await make_source(owner_engine, alpha, "a")
    hostile = RawEntry("h", "<i>x</i>", "x", "https://news.example/h", None, sha("h"))
    result, _, _ = await trial(worker_engine, slug, source, FakeConnector(ok(hostile)))
    assert (result.outcome, result.entries_seen, result.sample) == ("ok", 1, [])
