"""The protocol of the test fetch between the API and the worker (ADR-0013).

The worker handles hostile content, so what it leaves in Redis is read as untrusted data. These tests
attack the reader: sizes, types, extra fields, unsafe links, markup, look-alike answers meant for
another test.
"""

# ruff: noqa: E501

import json
import uuid
from typing import Any

import pytest

from pharos.ingest import testfetch

TEST = uuid.UUID("11111111-1111-4111-8111-111111111111")
SOURCE = uuid.UUID("22222222-2222-4222-8222-222222222222")


def good(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "v": 1,
        "test_id": str(TEST),
        "source_id": str(SOURCE),
        "outcome": "ok",
        "entries_seen": 12,
        "entries_skipped": 1,
        "bytes_fetched": 4096,
        "sample": [
            {
                "title": "Release 1.2",
                "url": "https://news.example.org/a",
                "published_at": "2026-10-20T10:00:00Z",
            },
            {"title": "No date", "url": "https://news.example.org/b"},
        ],
    }
    body.update(changes)
    return body


def read(document: Any) -> testfetch.TestResult | None:
    raw = document if isinstance(document, bytes | str) else json.dumps(document)
    return testfetch.parse_result(raw, test_id=TEST, source_id=SOURCE)


def test_a_good_answer_is_read() -> None:
    result = read(good())
    assert result is not None
    assert (result.outcome, result.entries_seen, len(result.sample)) == ("ok", 12, 2)
    assert result.sample[0].title == "Release 1.2" and result.sample[1].published_at is None


def test_what_the_worker_writes_is_what_the_api_reads() -> None:
    answer = testfetch.TestResult.model_validate(good())
    assert read(testfetch.encode(answer)) == answer
    assert read(testfetch.encode(answer).encode()) == answer  # Redis hands out bytes


@pytest.mark.parametrize(
    "outcome",
    [
        "ok",
        "blocked",
        "timeout",
        "too_large",
        "bad_status",
        "bad_content",
        "network",
        "error",
        "host_busy",
        "source_missing",
    ],
)
def test_every_outcome_of_the_protocol_is_accepted(outcome: str) -> None:
    assert read(good(outcome=outcome, sample=[])) is not None


@pytest.mark.parametrize(
    "document",
    [
        good(outcome="success"),
        good(outcome="OK"),
        good(outcome=None),
        good(outcome="<script>alert(1)</script>"),
        good(v=2),
        good(v="1"),
        good(error_code="Not A Word"),
        good(error_code="x" * 65),
        good(error_code="a b"),
        good(error_code=5),
        good(http_status=99),
        good(http_status=600),
        good(http_status="200"),
        good(entries_seen=-1),
        good(entries_seen=10**9),
        good(entries_seen=1.5),
        good(entries_seen=True),
        good(bytes_fetched=-5),
        good(unknown_field="x"),
        good(sample="not a list"),
        good(sample=[{"title": "t", "url": "https://a.example/", "extra": 1}]),
        good(sample=[{"title": "t"}]),
        good(sample=[{"url": "https://a.example/"}]),
        good(sample=["https://a.example/"]),
        good(sample=[{"title": "t", "url": "https://a.example/"}] * 6),
        good(sample=[{"title": "x" * 301, "url": "https://a.example/"}]),
        good(sample=[{"title": "t", "url": "https://a.example/" + "a" * 2100}]),
        good(sample=[{"title": "t", "url": "https://a.example/", "published_at": "yesterday"}]),
    ],
)
def test_an_answer_outside_the_protocol_is_not_read(document: dict[str, Any]) -> None:
    assert read(document) is None


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,x",
        "file:///etc/passwd",
        "ftp://a.example/",
        "//a.example/x",
        "/relative",
        "",
        "https://u:p@a.example/",
        "https://a.example/a b",
        'https://a.example/"x',
        "https://a.example/\ud800",
    ],
)
def test_a_sample_link_that_is_not_a_plain_web_link_makes_the_answer_unreadable(url: str) -> None:
    assert read(good(sample=[{"title": "t", "url": url}])) is None


@pytest.mark.parametrize("title", ["", "   ", "​‮", "\x00\x01", "<b></b>"])
def test_a_sample_title_with_nothing_readable_in_it_makes_the_answer_unreadable(title: str) -> None:
    assert read(good(sample=[{"title": title, "url": "https://a.example/"}])) is None


@pytest.mark.parametrize(
    "title",
    [
        "<b>Big</b> news",
        "news\u200b",
        "a\u202eb",
        "news\x00",
        "  padded  ",
        "a  b",
        "x\ty",
        "&lt;i&gt;",
    ],
)
def test_a_title_that_cleaning_would_change_makes_the_answer_unreadable(title: str) -> None:
    # the reader does not repair text: it refuses text that is not already what cleaning leaves
    assert read(good(sample=[{"title": title, "url": "https://a.example/"}])) is None


@pytest.mark.parametrize(
    "title",
    [
        "Release 1.2",
        "Zeitung: Ausgabe 5 \u2013 Wirtschaft",
        "Azərbaycan xəbərləri",
        "Новости",
        "a & b",
        "100%",
    ],
)
def test_a_plain_title_is_read_as_it_is(title: str) -> None:
    result = read(good(sample=[{"title": title, "url": "https://a.example/"}]))
    assert result is not None and result.sample[0].title == title


def test_an_answer_meant_for_another_test_or_another_source_is_not_this_ones() -> None:
    other = uuid.uuid4()
    assert read(good(test_id=str(other))) is None
    assert read(good(source_id=str(other))) is None
    assert testfetch.parse_result(json.dumps(good()), test_id=other, source_id=SOURCE) is None
    assert testfetch.parse_result(json.dumps(good()), test_id=TEST, source_id=other) is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        b"",
        "",
        b"not json",
        b"{",
        b"[]",
        b"null",
        b"5",
        b'"text"',
        b"true",
        b"\xff\xfe{}",
        b'{"v":',
        b"[" * 100_000,
        b"{" * 100_000,
        b'{"a":' * 100_000,
        b"1" * 5000,
    ],
)
def test_something_that_is_not_a_json_object_is_not_read(raw: bytes | str | None) -> None:
    assert testfetch.parse_result(raw, test_id=TEST, source_id=SOURCE) is None


def test_an_answer_over_the_size_limit_is_not_even_parsed() -> None:
    padded = good(note="x" * testfetch.MAX_RAW_BYTES)  # would be refused for the field anyway
    assert read(json.dumps(padded)) is None
    just_under = json.dumps(good(sample=[]))
    assert len(just_under) < testfetch.MAX_RAW_BYTES and read(just_under) is not None
    # a valid answer padded with white space past the limit is refused on size alone
    assert read(json.dumps(good()) + " " * testfetch.MAX_RAW_BYTES) is None
    assert read((json.dumps(good()) + " " * testfetch.MAX_RAW_BYTES).encode()) is None


def test_the_keys_are_under_the_prefixes_each_side_may_use() -> None:
    assert testfetch.result_key(TEST).startswith(
        "pharos:worker:"
    )  # the worker's Redis user owns it
    assert testfetch.marker_key(TEST).startswith("pharos:api:")  # the worker's user cannot reach it
    assert testfetch.cooldown_key(SOURCE, TEST).startswith("pharos:api:")
    assert testfetch.rate_key(ORG := uuid.uuid4()).startswith("pharos:api:") and str(
        ORG
    ) in testfetch.rate_key(ORG)
    assert testfetch.result_key(TEST) != testfetch.marker_key(TEST)
    assert str(TEST) in testfetch.result_key(TEST) and str(TEST) in testfetch.marker_key(TEST)
    # a cooldown is per organization and per source
    assert testfetch.cooldown_key(SOURCE, TEST) != testfetch.cooldown_key(TEST, SOURCE)


def test_the_limits_are_the_ones_the_decision_states() -> None:
    assert testfetch.COOLDOWN_SECONDS == 30
    assert testfetch.ORG_TESTS_PER_MINUTE == 10 and testfetch.RATE_WINDOW_SECONDS == 60
    assert testfetch.MAX_SAMPLES == 5
    assert testfetch.RESULT_TTL_SECONDS == 120 and testfetch.MARKER_TTL_SECONDS == 300
    assert testfetch.TASK_EXPIRES_SECONDS == 60
    assert testfetch.TASK_NAME == "pharos.ingest.test_fetch"
