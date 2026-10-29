"""The Hacker News (Algolia) connector against a scripted network."""

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from pharos.ingest.connectors import CONNECTORS, ConfigError, ConnectorError, FetchState
from pharos.ingest.connectors.hackernews import HackerNewsConnector
from pharos.net.addresses import AddressPolicy
from pharos.net.safe_http import SafeHttpClient
from tests.net.fakes import PUBLIC, FakeResolver, Script, response

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
pytestmark = pytest.mark.anyio
CONNECTOR = HackerNewsConnector()
CONFIG = {"query": "sap", "min_points": 20}
HOSTS: dict = {"hn.algolia.com": [PUBLIC]}
EPOCH = int(datetime(2026, 10, 20, 10, 0, tzinfo=UTC).timestamp())


def client(*answers: bytes) -> tuple[SafeHttpClient, Script]:
    script = Script(*answers)
    return SafeHttpClient(
        AddressPolicy(), resolver=FakeResolver(dict(HOSTS)), backend=script
    ), script  # type: ignore[arg-type]


def hit(n: int = 1, **fields: object) -> dict:
    return {
        "objectID": str(40_000_000 + n),
        "title": f"Story {n}",
        "url": f"https://blog.example/post/{n}",
        "story_text": None,
        "created_at_i": EPOCH,
        "points": 50,
        **fields,
    }


def ok(*hits: object, **headers: str) -> bytes:
    return response(200, json.dumps({"hits": list(hits)}).encode(), headers)


async def fetch(http: SafeHttpClient, config: dict | None = None, state: FetchState | None = None):  # type: ignore[no-untyped-def]
    return await CONNECTOR.fetch(config or CONFIG, state or FetchState(), http, NOW)


def test_the_connector_is_registered() -> None:
    assert CONNECTORS["hackernews"].type == "hackernews"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"query": "sap"}, {"query": "sap", "min_points": 20}),
        (
            {"query": " Finance close  ", "min_points": 0},
            {"query": "Finance close", "min_points": 0},
        ),
        ({"query": "x" * 100, "min_points": 10_000}, {"query": "x" * 100, "min_points": 10_000}),
        ({"query": "SAP S/4HANA"}, {"query": "SAP S/4HANA", "min_points": 20}),
        ({"query": "azərbaycan"}, {"query": "azərbaycan", "min_points": 20}),
    ],
)
def test_a_valid_configuration_is_normalized(raw: dict, expected: dict) -> None:
    assert CONNECTOR.validate_config(raw) == expected


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({}, "query_required"),
        ({"min_points": 5}, "query_required"),
        ({"query": "sap", "url": "https://evil.example/"}, "unknown_setting"),
        ({"query": ""}, "query_invalid"),
        ({"query": "   "}, "query_invalid"),
        ({"query": "x" * 101}, "query_invalid"),
        ({"query": "<script>alert(1)</script>"}, "query_invalid"),
        ({"query": "a\x00b"}, "query_invalid"),
        ({"query": "a\nb"}, "query_invalid"),
        ({"query": "a‮b"}, "query_invalid"),
        ({"query": "a​b"}, "query_invalid"),
        ({"query": 5}, "query_invalid"),
        ({"query": None}, "query_invalid"),
        ({"query": ["sap"]}, "query_invalid"),
        ({"query": "sap", "min_points": -1}, "min_points_invalid"),
        ({"query": "sap", "min_points": 10_001}, "min_points_invalid"),
        ({"query": "sap", "min_points": 1.5}, "min_points_invalid"),
        ({"query": "sap", "min_points": "20"}, "min_points_invalid"),
        ({"query": "sap", "min_points": True}, "min_points_invalid"),
        ({"query": "sap", "min_points": None}, "min_points_invalid"),
    ],
)
def test_an_invalid_configuration_is_refused_with_a_fixed_code(raw: dict, code: str) -> None:
    with pytest.raises(ConfigError) as caught:
        CONNECTOR.validate_config(raw)
    assert caught.value.code == code


def test_the_same_search_is_one_source_whatever_its_case() -> None:
    a = CONNECTOR.dedupe_key({"query": "SAP", "min_points": 20})
    assert a == CONNECTOR.dedupe_key({"query": "sap", "min_points": 20})
    assert a != CONNECTOR.dedupe_key({"query": "sap", "min_points": 21})
    assert a != CONNECTOR.dedupe_key({"query": "sap hana", "min_points": 20})


async def test_the_request_is_one_search_with_escaped_words() -> None:
    http, script = client(ok(hit()))
    await fetch(http, {"query": "a&b=c #d?e", "min_points": 7})
    line = script.streams[0].text.splitlines()[0]
    assert line.startswith("GET /api/v1/search_by_date?") and line.endswith(" HTTP/1.1")
    params = parse_qs(urlsplit(line.split(" ")[1]).query)
    assert params == {
        "query": ["a&b=c #d?e"],
        "tags": ["story"],
        "numericFilters": ["points>=7"],
        "hitsPerPage": ["30"],
    }
    assert "Host: hn.algolia.com" in script.streams[0].text
    assert "Authorization" not in script.streams[0].text


async def test_stories_become_entries() -> None:
    http, _ = client(
        ok(hit(1), hit(2, url=None, story_text="<p>Ask HN: how do you close the books?</p>"))
    )
    outcome = await fetch(http)
    first, second = outcome.entries
    assert (first.external_id, first.title, first.url) == (
        "40000001",
        "Story 1",
        "https://blog.example/post/1",
    )
    assert first.published_at == datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    assert second.url == "https://news.ycombinator.com/item?id=40000002"  # no link: the discussion
    assert second.excerpt == "Ask HN: how do you close the books?"
    assert (outcome.entries_seen, outcome.entries_skipped) == (2, 0)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,x",
        "//evil.example/x",
        "",
        5,
        "https://u:p@evil.example/",
    ],
)
async def test_an_unsafe_story_link_falls_back_to_the_discussion_page(url: object) -> None:
    http, _ = client(ok(hit(1, url=url)))
    [only] = (await fetch(http)).entries
    assert only.url == "https://news.ycombinator.com/item?id=40000001"


@pytest.mark.parametrize(
    "object_id", [None, 5, "", "abc", "12 3", "../../x", "1" * 13, "-1", "1\n2", "١٢٣"]
)
async def test_a_story_with_a_strange_identifier_is_dropped(object_id: object) -> None:
    http, _ = client(ok(hit(1, objectID=object_id), hit(2)))
    outcome = await fetch(http)
    assert [e.external_id for e in outcome.entries] == ["40000002"] and outcome.entries_skipped == 1


@pytest.mark.parametrize("title", [None, "", "   ", 5, [], "​‮", "<br>"])
async def test_a_story_without_a_readable_title_is_dropped(title: object) -> None:
    http, _ = client(ok(hit(1, title=title), hit(2)))
    assert [e.external_id for e in (await fetch(http)).entries] == ["40000002"]


@pytest.mark.parametrize(
    "stamp", [None, "2026-10-20", -1, 0, 10**12, 10**30, 1.5, True, "x", 4_000_000_000]
)
async def test_an_implausible_time_is_no_time(stamp: object) -> None:
    http, _ = client(ok(hit(1, created_at_i=stamp)))
    [only] = (await fetch(http)).entries
    assert only.published_at is None


@pytest.mark.parametrize("item", [None, 5, "x", [], True, {}, {"objectID": "1"}])
async def test_an_item_of_the_wrong_shape_never_crashes_the_run(item: object) -> None:
    http, _ = client(ok(item, hit(2)))
    outcome = await fetch(http)
    assert [e.external_id for e in outcome.entries] == ["40000002"] and outcome.entries_seen == 2


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"nope",
        b"[]",
        b'{"hits": null}',
        b'{"hits": {}}',
        b'{"hits": "x"}',
        b'{"nbHits": 3}',
        b"null",
        b"{" * 5000,
        b"[" * 5000,
        b'{"hits":' + b"[" * 1_000_000 + b"]" * 1_000_000 + b"}",  # too deep to read at all
        b"\xff\xfe{}",
    ],
    ids=range(12),
)
async def test_a_hostile_document_is_a_coarse_error(body: bytes) -> None:
    http, _ = client(response(200, body))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http)
    assert caught.value.outcome == "bad_content" and caught.value.code in {
        "json_invalid",
        "json_shape",
    }


async def test_at_most_the_entry_cap_is_looked_at() -> None:
    http, _ = client(ok(*[hit(n) for n in range(1, 301)]))
    outcome = await fetch(http)
    assert len(outcome.entries) == outcome.entries_seen == 200


async def test_a_stored_configuration_that_was_tampered_with_is_not_used() -> None:
    http, script = client(ok(hit()))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http, {"query": "sap&hitsPerPage=1000", "min_points": "0 OR 1", "extra": 1})
    assert (caught.value.outcome, caught.value.code) == ("error", "config_invalid")
    assert script.connects == []


async def test_validators_are_used_when_the_server_sends_them() -> None:
    http, script = client(response(304, b""))
    outcome = await fetch(http, state=FetchState('"v1"', None))
    assert outcome.kind == "not_modified" and outcome.etag == '"v1"'
    assert 'If-None-Match: "v1"' in script.streams[0].text


async def test_a_busy_server_is_a_failed_run_that_obeys_retry_after() -> None:
    http, _ = client(response(429, b"", {"Retry-After": "120"}))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http)
    assert (caught.value.outcome, caught.value.http_status, caught.value.retry_after) == (
        "bad_status",
        429,
        120,
    )
