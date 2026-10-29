"""SafeHttpClient against a scripted network: what it connects to, follows, sends and refuses."""

import asyncio
from collections.abc import Callable

import pytest

from pharos.net.addresses import AddressPolicy
from pharos.net.safe_http import (
    Bearer,
    FetchError,
    FetchErrorKind,
    FetchResult,
    SafeHttpClient,
    _PinnedBackend,
)

from .fakes import OTHER_PUBLIC, PUBLIC, FakeResolver, RecordingStream, Script, redirect, response

HOSTS: dict[str, list[str] | Callable[[int], list[str]]] = {
    "feed.example": [PUBLIC],
    "other.example": [OTHER_PUBLIC],
    "internal.example": ["10.1.2.3"],
    "metadata.example": ["169.254.169.254"],
    "loop.example": ["127.0.0.1"],
    "localhost": ["127.0.0.1", "::1"],
    "mixed.example": [PUBLIC, "10.0.0.5"],
    "v6.example": ["2606:4700:4700::1111"],
    "mapped.example": ["::ffff:127.0.0.1"],
}


def make(*answers: bytes | Callable[[], RecordingStream], **options: object):  # type: ignore[no-untyped-def]
    script = Script(*answers)
    resolver = FakeResolver(dict(HOSTS))
    client = SafeHttpClient(AddressPolicy(), resolver=resolver, backend=script, **options)  # type: ignore[arg-type]
    return client, script, resolver


async def blocked(client: SafeHttpClient, url: str) -> str:
    with pytest.raises(FetchError) as caught:
        await client.fetch(url)
    assert caught.value.kind is FetchErrorKind.BLOCKED, (url, caught.value.kind)
    return caught.value.reason


# --- connecting --------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_connection_goes_to_the_validated_address_not_the_name() -> None:
    client, script, resolver = make(response(200, b"hello", {"ETag": '"v1"'}))
    result = await client.fetch("http://feed.example/rss")
    assert isinstance(result, FetchResult)
    assert (result.status, result.body, result.url) == (200, b"hello", "http://feed.example/rss")
    assert result.headers["etag"] == '"v1"'
    assert script.connects == [(PUBLIC, 80)]
    assert resolver.calls == ["feed.example"]  # one lookup, used for the check AND the connect
    assert "Host: feed.example" in script.streams[0].text  # the request still names the host


@pytest.mark.anyio
async def test_tls_and_the_host_header_keep_the_original_name() -> None:
    client, script, _ = make(response(200, b"x"))
    await client.fetch("https://feed.example/rss")
    assert script.connects == [(PUBLIC, 443)]
    assert script.streams[0].tls_hostname == "feed.example"
    assert "Host: feed.example" in script.streams[0].text


@pytest.mark.anyio
async def test_ipv6_addresses_work() -> None:
    client, script, _ = make(response(200, b"x"))
    await client.fetch("http://v6.example/")
    assert script.connects == [("2606:4700:4700::1111", 80)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "host", ["internal.example", "metadata.example", "loop.example", "mapped.example"]
)
async def test_a_name_that_resolves_to_an_unsafe_address_is_never_connected_to(host: str) -> None:
    client, script, _ = make(response(200, b"secret"))
    await blocked(client, f"http://{host}/")
    assert script.connects == []


@pytest.mark.anyio
async def test_one_bad_address_among_good_ones_blocks_the_whole_name() -> None:
    client, script, _ = make(response(200, b"x"))
    assert (await blocked(client, "http://mixed.example/")).startswith("denied_range")
    assert script.connects == []


@pytest.mark.anyio
async def test_a_missing_name_looks_the_same_as_a_refused_one() -> None:
    """Otherwise a user could probe which internal names exist through the source form."""
    client, script, _ = make()
    with pytest.raises(FetchError) as missing:
        await client.fetch("http://nonexistent.example/")
    with pytest.raises(FetchError) as internal:
        await client.fetch("http://internal.example/")
    assert missing.value.kind is internal.value.kind is FetchErrorKind.BLOCKED
    assert script.connects == []


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["²", "٣", "9" * 5000, "-1", "1.5", "1e3", " "])
async def test_an_odd_retry_after_is_ignored_and_never_crashes(value: str) -> None:
    client, _, _ = make(response(429, b"", {"Retry-After": value}))
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/")
    assert caught.value.kind is FetchErrorKind.BAD_STATUS
    assert caught.value.retry_after is None


@pytest.mark.anyio
async def test_a_huge_retry_after_is_capped() -> None:
    client, _, _ = make(response(429, b"", {"Retry-After": "9999999"}))
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/")
    assert caught.value.retry_after == 7 * 24 * 3600


@pytest.mark.anyio
async def test_a_bearer_is_not_sent_to_a_look_alike_idn_host() -> None:
    """The standard library folds the sharp s to "ss"; httpx does not, so they are other hosts."""
    table: dict[str, list[str] | Callable[[int], list[str]]] = {
        "assets.example.org": [PUBLIC],
        "xn--aets-example-org-wfb.invalid": [OTHER_PUBLIC],
    }
    script = Script(response(200, b"x"))
    client = SafeHttpClient(AddressPolicy(), resolver=FakeResolver(table), backend=script)
    await client.fetch("https://assets.example.org/", bearer=Bearer("assets.example.org", "tok-1"))
    assert "tok-1" in script.streams[0].text
    from pharos.net.safe_http import check_url

    target = check_url("https://aßets.example.org/", frozenset())
    assert target.host != "assets.example.org"
    assert target.host.startswith("xn--")


@pytest.mark.anyio
async def test_rebinding_the_second_connection_is_checked_again() -> None:
    """The name answers with a public address first and a loopback address afterwards."""
    table: dict[str, list[str] | Callable[[int], list[str]]] = {
        "rebind.example": lambda call: [PUBLIC] if call == 1 else ["127.0.0.1"]
    }
    script = Script(response(200, b"first"), response(200, b"second"))
    resolver = FakeResolver(table)
    client = SafeHttpClient(AddressPolicy(), resolver=resolver, backend=script)
    assert (await client.fetch("http://rebind.example/")).body == b"first"
    assert await blocked(client, "http://rebind.example/") == "denied_range:127.0.0.0/8"
    assert script.connects == [(PUBLIC, 80)]  # the loopback was never connected to
    assert resolver.calls == ["rebind.example", "rebind.example"]


@pytest.mark.anyio
async def test_every_request_opens_a_fresh_checked_connection() -> None:
    client, script, resolver = make(response(200, b"a"), response(200, b"b"))
    await client.fetch("http://feed.example/1")
    await client.fetch("http://feed.example/2")
    assert len(script.connects) == 2
    assert len(resolver.calls) == 2


@pytest.mark.anyio
async def test_a_unix_socket_is_refused() -> None:
    from pharos.net.safe_http import SystemResolver, _Blocked

    backend = _PinnedBackend(Script(), SystemResolver(), AddressPolicy())
    with pytest.raises(_Blocked):
        await backend.connect_unix_socket("/var/run/docker.sock")


# --- redirects ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_redirect_to_a_safe_place_is_followed() -> None:
    client, script, _ = make(redirect("https://other.example/new"), response(200, b"moved"))
    result = await client.fetch("http://feed.example/old")
    assert (result.body, result.url) == (b"moved", "https://other.example/new")
    assert script.connects == [(PUBLIC, 80), (OTHER_PUBLIC, 443)]


@pytest.mark.anyio
async def test_a_relative_redirect_stays_on_the_same_host() -> None:
    client, script, _ = make(redirect("/other?x=1", 301), response(200, b"ok"))
    result = await client.fetch("http://feed.example/old")
    assert result.url == "http://feed.example/other?x=1"
    assert [c[0] for c in script.connects] == [PUBLIC, PUBLIC]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:7f00:1]/",
        "http://internal.example/",
        "http://metadata.example/",
        "ftp://feed.example/x",
        "file:///etc/passwd",
        "http://feed.example:6379/",
        "http://user:pw@other.example/",
        "gopher://feed.example/",
    ],
)
async def test_a_redirect_to_anything_unsafe_is_refused_at_the_first_hop(location: str) -> None:
    client, script, _ = make(redirect(location), response(200, b"never"))
    await blocked(client, "http://feed.example/start")
    assert script.connects == [(PUBLIC, 80)]  # only the first, harmless hop was made


@pytest.mark.anyio
@pytest.mark.parametrize("bad_hop", [1, 2, 3])
async def test_an_unsafe_redirect_later_in_the_chain_is_refused_too(bad_hop: int) -> None:
    chain = ["http://other.example/a", "http://feed.example/b", "http://other.example/c"]
    chain[bad_hop - 1] = "http://127.0.0.1/"
    client, script, _ = make(*[redirect(url) for url in chain], response(200, b"never"))
    await blocked(client, "http://feed.example/start")
    assert len(script.connects) == bad_hop  # one connection per safe hop, none to the bad one


@pytest.mark.anyio
async def test_a_fourth_redirect_is_one_too_many() -> None:
    hops = [redirect(f"http://feed.example/{n}") for n in range(1, 5)]
    client, script, _ = make(*hops, response(200, b"never"))
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/0")
    assert (caught.value.kind, caught.value.reason) == (
        FetchErrorKind.BAD_STATUS,
        "too_many_redirects",
    )
    assert len(script.connects) == 4  # the start and three redirects


@pytest.mark.anyio
async def test_three_redirects_are_allowed() -> None:
    hops = [redirect(f"http://feed.example/{n}") for n in range(1, 4)]
    client, _, _ = make(*hops, response(200, b"done"))
    assert (await client.fetch("http://feed.example/0")).body == b"done"


@pytest.mark.anyio
async def test_a_redirect_without_a_location_is_an_error() -> None:
    client, _, _ = make(response(302, b"", {}))
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/")
    assert caught.value.reason == "redirect_without_location"


# --- credentials -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_bearer_token_goes_only_to_its_own_host_over_https() -> None:
    client, script, _ = make(response(200, b"x"))
    await client.fetch("https://feed.example/", bearer=Bearer("feed.example", "tok-123"))
    assert "Authorization: Bearer tok-123" in script.streams[0].text


@pytest.mark.anyio
async def test_a_bearer_token_is_never_sent_over_plain_http() -> None:
    client, script, _ = make(response(200, b"x"))
    await client.fetch("http://feed.example/", bearer=Bearer("feed.example", "tok-123"))
    assert "tok-123" not in script.streams[0].text


@pytest.mark.anyio
async def test_a_bearer_token_is_dropped_on_a_redirect_to_another_host() -> None:
    client, script, _ = make(redirect("https://other.example/x"), response(200, b"x"))
    await client.fetch("https://feed.example/", bearer=Bearer("feed.example", "tok-123"))
    assert "tok-123" in script.streams[0].text
    assert "tok-123" not in script.streams[1].text
    assert "Authorization" not in script.streams[1].text


@pytest.mark.anyio
async def test_a_bearer_token_follows_a_redirect_within_its_host() -> None:
    client, script, _ = make(redirect("https://feed.example/y"), response(200, b"x"))
    await client.fetch("https://feed.example/x", bearer=Bearer("Feed.Example", "tok-123"))
    assert "tok-123" in script.streams[1].text


def test_a_bearer_never_shows_its_token_in_a_repr() -> None:
    assert "tok-123" not in repr(Bearer("feed.example", "tok-123"))


# --- what the caller may send --------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name", ["Authorization", "Host", "Cookie", "Accept-Encoding", "X-Forwarded-For"]
)
async def test_a_caller_cannot_set_sensitive_request_headers(name: str) -> None:
    client, script, _ = make(response(200, b"x"))
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/", headers={name: "evil"})
    assert caught.value.reason == "header_not_allowed"
    assert script.connects == []


@pytest.mark.anyio
async def test_conditional_headers_are_sent_and_compression_is_always_off() -> None:
    client, script, _ = make(response(304, b"", {}))
    result = await client.fetch(
        "http://feed.example/",
        headers={"If-None-Match": '"v1"', "If-Modified-Since": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )
    text = script.streams[0].text
    assert result.status == 304
    assert result.body == b""
    assert 'If-None-Match: "v1"' in text
    assert "If-Modified-Since: Wed, 21 Oct 2026" in text
    assert "Accept-Encoding: identity" in text
    assert "User-Agent: Pharos/" in text


@pytest.mark.anyio
async def test_environment_proxies_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    client, script, _ = make(response(200, b"direct"))
    assert (await client.fetch("http://feed.example/")).body == b"direct"
    assert script.connects == [(PUBLIC, 80)]  # not via the "proxy"


# --- answers -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_size_limit_is_exact() -> None:
    limit = 1000
    client, _, _ = make(
        response(200, b"a" * limit), response(200, b"a" * (limit + 1)), max_bytes=limit
    )
    assert len((await client.fetch("http://feed.example/")).body) == limit
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/")
    assert caught.value.kind is FetchErrorKind.TOO_LARGE


@pytest.mark.anyio
async def test_the_default_limit_is_two_mebibytes() -> None:
    two_mib = 2 * 1024 * 1024
    client, _, _ = make(response(200, b"a" * two_mib), response(200, b"a" * (two_mib + 1)))
    assert len((await client.fetch("http://feed.example/")).body) == two_mib
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/")
    assert caught.value.kind is FetchErrorKind.TOO_LARGE


@pytest.mark.anyio
async def test_a_large_declared_length_is_refused_before_reading() -> None:
    answer = response(200, b"", {"Content-Length": str(10**9)}, length=False)
    client, script, _ = make(answer)
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/")
    assert (caught.value.kind, caught.value.reason) == (FetchErrorKind.TOO_LARGE, "content_length")
    assert script.streams[0].written  # we asked, but never read a body


@pytest.mark.anyio
async def test_a_body_without_a_length_is_still_capped() -> None:
    chunked = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        + (b"%x\r\n" % 600 + b"a" * 600 + b"\r\n") * 4
        + b"0\r\n\r\n"
    )
    client, _, _ = make(chunked, max_bytes=2000)
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/")
    assert caught.value.kind is FetchErrorKind.TOO_LARGE


@pytest.mark.anyio
@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "GZIP", "gzip, identity"])
async def test_a_compressed_answer_is_refused_and_never_inflated(encoding: str) -> None:
    import gzip

    bomb = gzip.compress(b"\0" * 20_000_000)
    client, _, _ = make(response(200, bomb, {"Content-Encoding": encoding}))
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/")
    assert (caught.value.kind, caught.value.reason) == (FetchErrorKind.BAD_CONTENT, "compressed")


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 500, 502, 503])
async def test_other_statuses_are_errors(status: int) -> None:
    client, _, _ = make(response(status, b"nope"))
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/")
    assert caught.value.kind is FetchErrorKind.BAD_STATUS
    assert caught.value.reason == f"status_{status}"


@pytest.mark.anyio
async def test_retry_after_is_passed_on() -> None:
    client, _, _ = make(
        response(429, b"", {"Retry-After": "120"}), response(503, b"", {"Retry-After": "tomorrow"})
    )
    with pytest.raises(FetchError) as first:
        await client.fetch("http://feed.example/")
    with pytest.raises(FetchError) as second:
        await client.fetch("http://feed.example/")
    assert first.value.retry_after == 120
    assert second.value.retry_after is None  # a date or garbage is not trusted as a number


@pytest.mark.anyio
async def test_a_connection_that_never_answers_ends_at_the_deadline() -> None:
    client, _, _ = make(lambda: RecordingStream([], hang=True), deadline_seconds=0.3)
    started = asyncio.get_running_loop().time()
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/")
    assert caught.value.kind is FetchErrorKind.TIMEOUT
    assert asyncio.get_running_loop().time() - started < 2


@pytest.mark.anyio
async def test_errors_do_not_contain_the_url_or_the_address() -> None:
    client, _, _ = make()
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://internal.example/secret-path?token=abc")
    text = f"{caught.value!s} {caught.value.reason}"
    assert "secret-path" not in text
    assert "token=abc" not in text
    assert "10.1.2.3" not in text


# --- a spent rate limit (GitHub) ----


def limited(status: int, remaining: str, reset: str, **extra: str) -> bytes:
    headers = {"X-RateLimit-Remaining": remaining, "X-RateLimit-Reset": reset, **extra}
    return response(status, b"", headers)


async def retry_after_of(answer: bytes, now: float = 1_000_000.0) -> int | None:
    client, _, _ = make(answer, clock=lambda: now)
    with pytest.raises(FetchError) as caught:
        await client.fetch("http://feed.example/")
    assert caught.value.kind is FetchErrorKind.BAD_STATUS
    return caught.value.retry_after


@pytest.mark.anyio
@pytest.mark.parametrize("status", [403, 429])
async def test_a_spent_rate_limit_waits_until_it_is_renewed(status: int) -> None:
    assert await retry_after_of(limited(status, "0", "1000600")) == 601  # 600 s and a second


@pytest.mark.anyio
async def test_a_retry_after_header_wins_over_the_rate_limit_reset() -> None:
    assert await retry_after_of(limited(429, "0", "1000600", **{"Retry-After": "30"})) == 30


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "remaining", "reset"),
    [
        (403, "7", "1000600"),  # not spent: a plain refusal, not a limit
        (403, "", "1000600"),
        (404, "0", "1000600"),  # only 403 and 429 can mean "limited"
        (500, "0", "1000600"),
        (403, "0", "soon"),
        (403, "0", "-5"),
        (403, "0", "9" * 40),
        (403, "0", ""),
    ],
)
async def test_anything_else_is_not_read_as_a_rate_limit(
    status: int, remaining: str, reset: str
) -> None:
    assert await retry_after_of(limited(status, remaining, reset)) is None


@pytest.mark.anyio
async def test_a_reset_in_the_past_waits_one_second_and_a_far_one_is_capped() -> None:
    assert await retry_after_of(limited(403, "0", "999000")) == 1
    assert await retry_after_of(limited(403, "0", str(10**11))) == 7 * 24 * 3600


@pytest.mark.anyio
async def test_the_rate_limit_headers_reach_the_caller_of_a_good_answer() -> None:
    client, _, _ = make(limited(200, "42", "1000600"))
    result = await client.fetch("http://feed.example/")
    assert result.headers["x-ratelimit-remaining"] == "42"
    assert result.headers["x-ratelimit-reset"] == "1000600"
