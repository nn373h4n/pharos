# ruff: noqa: E501
"""The GitHub releases connector against a scripted network: requests, the token and hostile JSON."""

import json
from datetime import UTC, datetime

import pytest

from pharos.ingest.connectors import (
    CONNECTORS,
    ConfigError,
    ConnectorError,
    FetchState,
    build_connectors,
)
from pharos.ingest.connectors.github import GithubConnector
from pharos.net.addresses import AddressPolicy
from pharos.net.safe_http import SafeHttpClient
from tests.net.fakes import PUBLIC, FakeResolver, Script, redirect, response

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
pytestmark = pytest.mark.anyio
TOKEN = "github_pat_11AAAAAAA0abcdefghijkl_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
CONFIG = {"repo": "acme/widget"}
HOSTS: dict = {"api.github.com": [PUBLIC], "evil.example": [PUBLIC]}


def client(*answers: bytes, **options: object) -> tuple[SafeHttpClient, Script]:
    script = Script(*answers)
    return SafeHttpClient(
        AddressPolicy(), resolver=FakeResolver(dict(HOSTS)), backend=script, **options
    ), script  # type: ignore[arg-type]


def release(n: int = 1, **fields: object) -> dict:
    return {
        "id": 1000 + n,
        "tag_name": f"v1.{n}.0",
        "name": f"Version 1.{n}",
        "body": f"Release notes {n}",
        "html_url": f"https://github.com/acme/widget/releases/tag/v1.{n}.0",
        "published_at": "2026-10-20T10:00:00Z",
        "draft": False,
        "prerelease": False,
        **fields,
    }


def ok(*releases: object, **headers: str) -> bytes:
    return response(200, json.dumps(list(releases)).encode(), {"ETag": '"abc"', **headers})


async def fetch(
    http: SafeHttpClient, connector: GithubConnector | None = None, state: FetchState | None = None
):  # type: ignore[no-untyped-def]
    return await (connector or GithubConnector()).fetch(CONFIG, state or FetchState(), http, NOW)


# --- configuration ---------------------------------------------------------------------------------------


def test_the_connector_is_registered_without_a_credential() -> None:
    assert CONNECTORS["github"].type == "github"
    assert "set" not in repr(CONNECTORS["github"])


@pytest.mark.parametrize(
    "repo", ["acme/widget", "A/b", "a-b/c.d_e-f", "x" * 39 + "/" + "y" * 100, "0/1", "acme/.github"]
)
def test_a_valid_repository(repo: str) -> None:
    assert GithubConnector().validate_config({"repo": repo}) == {"repo": repo}


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({}, "repo_required"),
        ({"url": "https://github.com/acme/widget"}, "repo_required"),
        ({"repo": "acme/widget", "token": "x"}, "unknown_setting"),
        ({"repo": "acme/widget", "per_page": 100}, "unknown_setting"),
        ({"repo": ""}, "repo_invalid"),
        ({"repo": "acme"}, "repo_invalid"),
        ({"repo": "acme/"}, "repo_invalid"),
        ({"repo": "/widget"}, "repo_invalid"),
        ({"repo": "acme/widget/extra"}, "repo_invalid"),
        ({"repo": "acme/wid get"}, "repo_invalid"),
        ({"repo": "acme/widget?x=1"}, "repo_invalid"),
        ({"repo": "acme/widget#frag"}, "repo_invalid"),
        ({"repo": "acme/widget\n"}, "repo_invalid"),
        ({"repo": "acme/wid%67et"}, "repo_invalid"),
        ({"repo": "acme/.."}, "repo_invalid"),
        ({"repo": "acme/."}, "repo_invalid"),
        ({"repo": "acme/widget.git"}, "repo_invalid"),
        ({"repo": "-acme/widget"}, "repo_invalid"),
        ({"repo": "ac--me/widget"}, "repo_invalid"),
        ({"repo": "acme-/widget"}, "repo_invalid"),
        ({"repo": "x" * 40 + "/widget"}, "repo_invalid"),
        ({"repo": "acme/" + "y" * 101}, "repo_invalid"),
        ({"repo": "acme@evil.example/widget"}, "repo_invalid"),
        ({"repo": "\u0430cme/widget"}, "repo_invalid"),  # a look-alike letter
        ({"repo": 5}, "repo_invalid"),
        ({"repo": None}, "repo_invalid"),
        ({"repo": ["acme/widget"]}, "repo_invalid"),
    ],
)
def test_an_invalid_configuration_is_refused_with_a_fixed_code(raw: dict, code: str) -> None:
    with pytest.raises(ConfigError) as caught:
        GithubConnector().validate_config(raw)
    assert caught.value.code == code


def test_two_spellings_of_one_repository_are_one_source() -> None:
    c = GithubConnector()
    assert c.dedupe_key({"repo": "Acme/Widget"}) == c.dedupe_key({"repo": "acme/widget"})
    assert c.dedupe_key({"repo": "acme/widget"}) != c.dedupe_key({"repo": "acme/other"})


def test_the_source_is_always_fetched_from_one_host() -> None:
    assert GithubConnector().politeness_host(CONFIG) == "api.github.com"


# --- the request -----------------------------------------------------------------------------------------


async def test_the_request_asks_for_releases_of_that_repository_only() -> None:
    http, script = client(ok(release()))
    await fetch(http)
    sent = script.streams[0].text
    assert sent.startswith("GET /repos/acme/widget/releases?per_page=30 HTTP/1.1")
    assert "Host: api.github.com" in sent
    assert "Accept: application/vnd.github+json" in sent
    assert "Authorization" not in sent  # no token configured


async def test_the_token_goes_to_api_github_com_over_https_as_a_bearer() -> None:
    http, script = client(ok(release()))
    await fetch(http, GithubConnector(TOKEN))
    assert f"Authorization: Bearer {TOKEN}" in script.streams[0].text
    assert script.streams[0].tls_hostname == "api.github.com"


async def test_a_redirect_to_another_host_never_carries_the_token() -> None:
    http, script = client(
        redirect("https://evil.example/repos/acme/widget/releases"), ok(release())
    )
    await fetch(http, GithubConnector(TOKEN))
    assert f"Bearer {TOKEN}" in script.streams[0].text
    assert "Authorization" not in script.streams[1].text
    assert TOKEN not in script.streams[1].text


async def test_the_token_is_never_in_an_error_a_repr_or_the_url() -> None:
    connector = GithubConnector(TOKEN)
    assert TOKEN not in repr(connector)
    http, script = client(response(401, b'{"message":"Bad credentials"}'))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http, connector)
    assert TOKEN not in str(caught.value) and TOKEN not in repr(caught.value)
    assert (caught.value.outcome, caught.value.code, caught.value.http_status) == (
        "bad_status",
        "http_error",
        401,
    )
    assert TOKEN not in script.streams[0].text.splitlines()[0]  # not in the request line


async def test_the_validators_of_the_last_fetch_are_sent_back() -> None:
    http, script = client(response(304, b""))
    outcome = await fetch(http, state=FetchState('"abc"', "Tue, 20 Oct 2026 10:00:00 GMT"))
    assert outcome.kind == "not_modified"
    assert (outcome.etag, outcome.last_modified) == ('"abc"', "Tue, 20 Oct 2026 10:00:00 GMT")
    assert 'If-None-Match: "abc"' in script.streams[0].text
    assert "If-Modified-Since: Tue, 20 Oct 2026 10:00:00 GMT" in script.streams[0].text


async def test_a_validator_with_control_characters_is_not_sent() -> None:
    http, script = client(ok(release()))
    await fetch(http, state=FetchState('"a"\r\nX-Injected: 1', "ok\x00"))
    assert "X-Injected" not in script.streams[0].text
    assert "If-None-Match" not in script.streams[0].text


@pytest.mark.parametrize(
    "repo", ["acme/widget/../../admin", "acme/..", "acme/.", "acme/widget.git", "", None, 5]
)
async def test_a_stored_configuration_that_was_tampered_with_is_not_used(repo: object) -> None:
    http, script = client(ok(release()))
    with pytest.raises(ConnectorError) as caught:
        await GithubConnector(TOKEN).fetch({"repo": repo}, FetchState(), http, NOW)
    assert (caught.value.outcome, caught.value.code) == ("error", "config_invalid")
    assert script.connects == []  # nothing was sent, with or without the credential


# --- what comes back -------------------------------------------------------------------------------------


async def test_releases_become_entries() -> None:
    http, _ = client(ok(release(1), release(2, name=None), release(3, body=None)))
    outcome = await fetch(http)
    assert outcome.kind == "ok" and outcome.etag == '"abc"'
    assert outcome.entries_seen == 3 and outcome.entries_skipped == 0
    first, second, third = outcome.entries
    assert (first.external_id, first.title, first.excerpt) == (
        "1001",
        "acme/widget: Version 1.1",
        "Release notes 1",
    )
    assert first.url == "https://github.com/acme/widget/releases/tag/v1.1.0"
    assert first.published_at == datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    assert second.title == "acme/widget: v1.2.0"  # the tag, when the release has no name
    assert third.excerpt == ""
    assert len({e.content_sha256 for e in outcome.entries}) == 3


async def test_drafts_and_pre_releases_are_skipped_and_counted() -> None:
    http, _ = client(ok(release(1), release(2, draft=True), release(3, prerelease=True)))
    outcome = await fetch(http)
    assert [e.external_id for e in outcome.entries] == ["1001"]
    assert (outcome.entries_seen, outcome.entries_skipped) == (3, 2)


async def test_an_empty_list_is_a_good_answer() -> None:
    http, _ = client(ok())
    outcome = await fetch(http)
    assert outcome.kind == "ok" and outcome.entries == () and outcome.entries_seen == 0


async def test_at_most_the_entry_cap_is_looked_at() -> None:
    http, _ = client(ok(*[release(n) for n in range(1, 301)]))
    outcome = await fetch(http)
    assert len(outcome.entries) == outcome.entries_seen == 200


async def test_markup_and_look_alike_text_in_a_release_come_out_as_plain_text() -> None:
    http, _ = client(
        ok(
            release(
                1,
                name="<b>Big</b>‮ release​",
                body="<script>alert(1)</script>Fixes &amp; more\x00" + "x" * 5000,
            )
        )
    )
    [only] = (await fetch(http)).entries
    assert "<" not in only.title and "‮" not in only.title and "​" not in only.title
    assert "<script>" not in only.excerpt and "\x00" not in only.excerpt
    assert len(only.excerpt) <= 500 and len(only.title) <= 300


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,x",
        "file:///etc/passwd",
        "//evil.example/x",
        "/relative",
        "",
        None,
        5,
        ["x"],
        "https://u:p@evil.example/",
    ],
)
async def test_a_release_without_a_plain_web_link_is_dropped(url: object) -> None:
    http, _ = client(ok(release(1, html_url=url), release(2)))
    outcome = await fetch(http)
    assert [e.external_id for e in outcome.entries] == ["1002"]
    assert outcome.entries_skipped == 1


@pytest.mark.parametrize(
    "published",
    [
        "2999-01-01T00:00:00Z",
        "1970-01-01T00:00:00Z",
        "yesterday",
        "2026-10-20T10:00:00",
        "9" * 500,
        5,
        None,
        True,
    ],
)
async def test_an_implausible_date_is_no_date_and_does_not_drop_the_release(
    published: object,
) -> None:
    http, _ = client(ok(release(1, published_at=published)))
    [only] = (await fetch(http)).entries
    assert only.published_at is None


@pytest.mark.parametrize(
    "item",
    [
        None,
        5,
        "text",
        [],
        True,
        {},
        {"id": 1},
        release(1, name=" ", tag_name=None),
        release(1, name=5, tag_name=7),
        release(1, id="x"),
        release(1, id=True),
        release(1, id=None),
    ],
)
async def test_an_item_of_the_wrong_shape_never_crashes_the_run(item: object) -> None:
    http, _ = client(ok(item, release(2)))
    outcome = await fetch(http)
    ids = [e.external_id for e in outcome.entries]
    assert "1002" in ids and outcome.entries_seen == 2
    assert all(len(e.external_id) <= 512 for e in outcome.entries)


NOT_JSON = [
    b"",
    b"not json",
    b"{",
    b"\xff\xfe[]",  # a byte order mark: JSON on the wire is UTF-8 and nothing else
    "[]".encode("utf-16"),
    "[]".encode("utf-32"),
    b"\xef\xbb\xbf[]",
    b"[" * 1_000_000 + b"]" * 1_000_000,  # nested deeper than Python can read (under 2 MiB)
    b'[{"a":' * 300_000,
    b"[1" + b"0" * 6000 + b"]",  # a number with thousands of digits
    b'["\xc3\x28"]',  # invalid UTF-8
]
NOT_A_LIST = [b'{"message": "Not Found"}', b"null", b"5", b'"text"', b"true", b"{}"]


@pytest.mark.parametrize("body", NOT_JSON, ids=range(len(NOT_JSON)))
async def test_a_document_that_is_not_json_is_a_coarse_error(body: bytes) -> None:
    http, _ = client(response(200, body))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http)
    assert (caught.value.outcome, caught.value.code) == ("bad_content", "json_invalid")


@pytest.mark.parametrize("body", NOT_A_LIST, ids=range(len(NOT_A_LIST)))
async def test_json_that_is_not_a_list_of_releases_is_a_coarse_error(body: bytes) -> None:
    http, _ = client(response(200, body))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http)
    assert (caught.value.outcome, caught.value.code) == ("bad_content", "json_shape")


async def test_nan_and_other_odd_values_inside_a_list_are_just_items_to_skip() -> None:
    http, _ = client(response(200, b"[NaN, Infinity, -Infinity, 1e999]"))
    outcome = await fetch(http)
    assert outcome.entries == () and outcome.entries_skipped == outcome.entries_seen == 4


async def test_a_spent_rate_limit_is_a_failed_run_that_waits_for_the_reset() -> None:
    http, _ = client(
        response(
            403,
            b'{"message":"rate limit"}',
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000600"},
        ),
        clock=lambda: 1_000_000.0,
    )
    with pytest.raises(ConnectorError) as caught:
        await fetch(http)
    assert (caught.value.outcome, caught.value.http_status, caught.value.retry_after) == (
        "bad_status",
        403,
        601,
    )


async def test_a_missing_repository_is_a_failed_run_with_the_status_only() -> None:
    http, _ = client(response(404, b'{"message":"Not Found","documentation_url":"https://x"}'))
    with pytest.raises(ConnectorError) as caught:
        await fetch(http)
    assert (caught.value.outcome, caught.value.code, caught.value.http_status) == (
        "bad_status",
        "http_error",
        404,
    )
    assert "Not Found" not in str(caught.value)


def test_build_connectors_hands_the_credential_to_the_github_connector_only() -> None:
    built = build_connectors(TOKEN)
    assert built.keys() == {"rss", "github", "hackernews"}
    assert built["github"]._token == TOKEN  # type: ignore[attr-defined]
    assert not any(hasattr(built[k], "_token") for k in ("rss", "hackernews"))
    assert build_connectors()["github"]._token is None  # type: ignore[attr-defined]
    assert build_connectors("")["github"]._token is None  # type: ignore[attr-defined]


async def test_a_list_nested_a_few_thousand_deep_is_read_and_its_items_are_skipped() -> None:
    http, _ = client(response(200, b"[" * 5000 + b"]" * 5000))
    outcome = await fetch(http)
    assert outcome.entries == () and outcome.entries_seen == 1
