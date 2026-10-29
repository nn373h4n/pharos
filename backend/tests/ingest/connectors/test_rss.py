# ruff: noqa: E501
"""The RSS connector end to end against a scripted network: requests, validators and failures."""

from datetime import UTC, datetime

import pytest

from pharos.ingest.connectors import CONNECTORS, ConfigError, ConnectorError, FetchState
from pharos.ingest.connectors.base import OUTCOMES, error_from_fetch
from pharos.ingest.connectors.rss import RssConnector, normalized_feed_url
from pharos.net.addresses import AddressPolicy
from pharos.net.safe_http import FetchError, FetchErrorKind, SafeHttpClient
from tests.net.fakes import PUBLIC, FakeResolver, Script, redirect, response

from .. import feeds

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
pytestmark = pytest.mark.anyio
CONNECTOR = RssConnector()
CONFIG = {"url": "https://news.example/feed.xml"}
HOSTS: dict = {
    "news.example": [PUBLIC],
    "internal.example": ["10.1.2.3"],
}


def client(*answers: bytes, **options: object) -> tuple[SafeHttpClient, Script]:
    script = Script(*answers)
    return SafeHttpClient(
        AddressPolicy(), resolver=FakeResolver(dict(HOSTS)), backend=script, **options
    ), script  # type: ignore[arg-type]


async def fetch(http: SafeHttpClient, state: FetchState | None = None, config: dict | None = None):  # type: ignore[no-untyped-def]
    return await CONNECTOR.fetch(config or CONFIG, state or FetchState(), http, NOW)


# --- configuration ---------------------------------------------------------------------------------------


def test_the_connector_is_registered() -> None:
    assert CONNECTORS["rss"].type == "rss"


@pytest.mark.parametrize(
    "url",
    ["https://news.example/feed.xml", "http://news.example/rss?x=1", "https://news.example:8443/f"],
)
def test_a_valid_configuration_is_normalized_to_just_the_url(url: str) -> None:
    assert CONNECTOR.validate_config({"url": url}) == {"url": url}


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({}, "url_required"),
        ({"link": "https://x.example/"}, "url_required"),
        ({"url": "https://x.example/", "token": "abc"}, "unknown_setting"),
        ({"url": "javascript:alert(1)"}, "url_invalid"),
        ({"url": "ftp://x.example/feed"}, "url_invalid"),
        ({"url": "https://user:pw@x.example/"}, "url_invalid"),
        ({"url": "/relative"}, "url_invalid"),
        ({"url": ""}, "url_invalid"),
        ({"url": None}, "url_invalid"),
        ({"url": 5}, "url_invalid"),
        ({"url": ["https://x.example/"]}, "url_invalid"),
        ({"url": "https://x.example/ a"}, "url_invalid"),
    ],
)
def test_an_invalid_configuration_is_refused_with_a_fixed_code(raw: dict, code: str) -> None:
    with pytest.raises(ConfigError) as caught:
        CONNECTOR.validate_config(raw)
    assert caught.value.code == code


def test_the_same_feed_written_differently_has_one_key() -> None:
    spellings = [
        "https://news.example/feed.xml",
        "HTTPS://NEWS.EXAMPLE/feed.xml",
        "https://news.example:443/feed.xml",
        "https://news.example./feed.xml",
        "https://news.example/feed.xml#top",
    ]
    keys = {CONNECTOR.dedupe_key({"url": url}) for url in spellings}
    assert len(keys) == 1 and len(next(iter(keys))) == 64
    assert CONNECTOR.dedupe_key({"url": "https://news.example/other.xml"}) not in keys
    assert CONNECTOR.dedupe_key({"url": "http://news.example/feed.xml"}) not in keys
    assert CONNECTOR.dedupe_key({"url": "https://news.example/feed.xml?a=1"}) not in keys


def test_normalizing_keeps_a_non_default_port_and_a_query() -> None:
    assert (
        normalized_feed_url("HTTPS://News.Example:8443/F?a=1#x")
        == "https://news.example:8443/F?a=1"
    )
    assert normalized_feed_url("http://news.example") == "http://news.example/"


# --- fetching --------------------------------------------------------------------------------------------


async def test_a_first_fetch_asks_without_validators_and_returns_entries_and_validators() -> None:
    http, script = client(
        response(200, feeds.RSS, {"ETag": '"v1"', "Last-Modified": "Tue, 27 Oct 2026 10:00:00 GMT"})
    )
    outcome = await fetch(http)
    text = script.streams[0].text
    assert "If-None-Match" not in text and "If-Modified-Since" not in text
    assert outcome.kind == "ok"
    assert [e.title for e in outcome.entries] == [
        "First story",
        "Second story",
        "Story with content only",
    ]
    assert (outcome.etag, outcome.last_modified) == ('"v1"', "Tue, 27 Oct 2026 10:00:00 GMT")
    assert outcome.bytes_fetched == len(feeds.RSS) and outcome.entries_seen == 3


async def test_a_later_fetch_sends_the_validators_and_a_304_means_nothing_new() -> None:
    http, script = client(response(304, b"", {}))
    state = FetchState('"v1"', "Tue, 27 Oct 2026 10:00:00 GMT")
    outcome = await fetch(http, state)
    text = script.streams[0].text
    assert 'If-None-Match: "v1"' in text and "If-Modified-Since: Tue, 27 Oct 2026" in text
    assert (outcome.kind, outcome.entries) == ("not_modified", ())
    assert (outcome.etag, outcome.last_modified) == ('"v1"', "Tue, 27 Oct 2026 10:00:00 GMT")


async def test_a_304_that_repeats_the_validators_replaces_them() -> None:
    http, _ = client(response(304, b"", {"ETag": '"v2"'}))
    outcome = await fetch(http, FetchState('"v1"', "old"))
    assert (outcome.etag, outcome.last_modified) == ('"v2"', "old")


async def test_a_200_without_validators_forgets_the_old_ones() -> None:
    http, _ = client(response(200, feeds.RSS, {}))
    outcome = await fetch(http, FetchState('"v1"', "old"))
    assert (outcome.etag, outcome.last_modified) == (None, None)  # so the next request is plain


async def test_only_the_feed_is_fetched_never_the_pages_it_links_to() -> None:
    http, script = client(response(200, feeds.RSS, {}))
    await fetch(http)
    assert len(script.connects) == 1


async def test_hostile_entries_are_cleaned_on_the_way_through() -> None:
    body = feeds.rss_with_entries(1, title="&lt;script&gt;alert(1)&lt;/script&gt;Hi", summary="x")
    http, _ = client(response(200, body, {}))
    outcome = await fetch(http)
    title = outcome.entries[0].title
    assert (
        title == "Hi 0"
    )  # feedparser already treats escaped markup as HTML and removes the script
    assert "script" not in title and "alert" not in title


# --- failures --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 404, 410, 500, 503])
async def test_an_error_status_is_a_bad_status_with_the_number(status: int) -> None:
    http, _ = client(response(status, b"nope"))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http)
    assert (caught.value.outcome, caught.value.code, caught.value.http_status) == (
        "bad_status",
        "http_error",
        status,
    )


async def test_retry_after_is_passed_on() -> None:
    http, _ = client(response(429, b"", {"Retry-After": "300"}))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http)
    assert (caught.value.http_status, caught.value.retry_after) == (429, 300)


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "http://internal.example/",
        "ftp://news.example/x",
    ],
)
async def test_a_redirect_to_somewhere_internal_is_blocked_and_says_nothing_about_where(
    location: str,
) -> None:
    http, script = client(redirect(location), response(200, feeds.RSS, {}))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http)
    assert (caught.value.outcome, caught.value.code) == ("blocked", "not_allowed")
    assert location not in str(caught.value) and "10.1.2.3" not in str(caught.value)
    assert len(script.connects) == 1


async def test_a_configured_internal_address_is_blocked_too() -> None:
    http, script = client(response(200, feeds.RSS, {}))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http, config={"url": "http://internal.example/feed.xml"})
    assert caught.value.outcome == "blocked" and script.connects == []


async def test_an_oversized_answer_is_too_large() -> None:
    http, _ = client(response(200, b"x" * 5000, {}), max_bytes=1000)
    with pytest.raises(ConnectorError) as caught:
        await fetch(http)
    assert (caught.value.outcome, caught.value.code) == ("too_large", "too_large")


async def test_a_compressed_answer_is_unreadable() -> None:
    http, _ = client(response(200, b"\x1f\x8b\x08", {"Content-Encoding": "gzip"}))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http)
    assert (caught.value.outcome, caught.value.code) == ("bad_content", "unreadable")


async def test_a_name_that_does_not_resolve_looks_the_same_as_a_refused_one() -> None:
    http, _ = client()
    with pytest.raises(ConnectorError) as caught:
        await fetch(http, config={"url": "https://nonexistent.example/feed"})
    assert (caught.value.outcome, caught.value.code) == ("blocked", "not_allowed")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (feeds.HTML_PAGE, "bad_content"),
        (feeds.BILLION_LAUGHS, "bad_content"),
        (feeds.EXTERNAL_FILE, "bad_content"),
        (b"", "bad_content"),
    ],
)
async def test_a_document_that_is_not_a_feed_is_bad_content(body: bytes, code: str) -> None:
    http, _ = client(response(200, body, {}))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http)
    assert (caught.value.outcome, caught.value.code) == ("bad_content", code)


# --- the error vocabulary ----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(FetchErrorKind))
def test_every_fetch_failure_maps_to_fixed_words(kind: FetchErrorKind) -> None:
    error = error_from_fetch(FetchError(kind, "denied_range:10.0.0.0/8"))
    assert error.outcome in OUTCOMES
    assert "10.0.0.0" not in str(error) and "denied" not in str(error)


def test_unexpected_words_become_a_generic_error_instead_of_leaking() -> None:
    error = ConnectorError("exploded", "Has Spaces And http://x.example/")
    assert (error.outcome, error.code) == ("error", "unexpected")
    assert "x.example" not in str(error)


@pytest.mark.parametrize(
    "value", ["caf\u00e9", "a\x00b", "x" * 513, "tab\there", "\u202eevil", "", "\x7f"]
)
async def test_validators_that_could_not_be_sent_back_safely_are_never_stored_or_sent(
    value: str,
) -> None:
    http, script = client(response(200, feeds.RSS, {}), response(200, feeds.RSS, {}))
    outcome = await fetch(http, FetchState(etag=value, last_modified=value))
    assert "If-None-Match" not in script.streams[0].text  # a stored bad value is not echoed
    assert "If-Modified-Since" not in script.streams[0].text
    assert outcome.kind == "ok"


async def test_a_server_cannot_make_us_store_an_unsendable_validator() -> None:
    http, _ = client(response(200, feeds.RSS, {"ETag": "x" * 600, "Last-Modified": "m" * 200}))
    outcome = await fetch(http)
    assert (outcome.etag, outcome.last_modified) == (None, None)
    http, _ = client(
        response(
            200, feeds.RSS, {"ETag": 'W/"abc-1"', "Last-Modified": "Tue, 27 Oct 2026 10:00:00 GMT"}
        )
    )
    outcome = await fetch(http)
    assert (outcome.etag, outcome.last_modified) == ('W/"abc-1"', "Tue, 27 Oct 2026 10:00:00 GMT")
