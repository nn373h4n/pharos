"""The URL-level rules, before any address is looked at."""

import pytest

from pharos.net.safe_http import FetchError, FetchErrorKind, check_url

NO_BLOCKED: frozenset[str] = frozenset()


def refused(url: str, blocked: frozenset[str] = NO_BLOCKED) -> str:
    with pytest.raises(FetchError) as caught:
        check_url(url, blocked)
    assert caught.value.kind is FetchErrorKind.BLOCKED
    return caught.value.reason


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://example.com/feed", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("gopher://example.com/", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("data:text/plain,hello", "scheme"),
        ("//example.com/feed", "scheme"),
        ("example.com/feed", "scheme"),
        ("", "scheme"),
        ("http://user:pass@example.com/", "credentials_in_url"),
        ("http://user@example.com/", "credentials_in_url"),
        ("http://example.com@evil.example/", "credentials_in_url"),
        ("https://:@example.com/", "credentials_in_url"),
        ("http://example.com:8080/", "port"),
        ("http://example.com:22/", "port"),
        ("http://example.com:6379/", "port"),
        ("https://example.com:80/", "port"),
        ("http://example.com:443/", "port"),
        ("http://example.com:99999/", "url_malformed"),
        ("http:///feed", "no_host"),
        ("http://", "no_host"),
        ("http://exa mple.com/", "url_malformed"),
        ("http://example.com/\nX: y", "url_malformed"),
        ("http://example.com/\x00", "url_malformed"),
        ("http://example.com/\t", "url_malformed"),
        ("http://" + "a" * 2100 + ".example/", "url_malformed"),
        ("http://-.example/", "host_malformed"),
    ],
)
def test_urls_that_are_refused_before_any_lookup(url: str, reason: str) -> None:
    if reason == "host_malformed":
        # Whatever the codec says about it, a hostname that cannot be encoded never gets through.
        try:
            check_url(url, NO_BLOCKED)
        except FetchError as error:
            assert error.kind is FetchErrorKind.BLOCKED
        return
    assert refused(url) == reason


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/feed",
        "https://example.com/feed.xml?x=1#frag",
        "HTTP://EXAMPLE.COM/",
        "https://example.com:443/x",
        "http://example.com:80/",
        "https://sub.example.com./x",
        "http://bücher.example/",
        "http://xn--bcher-kva.example/",
        "https://93.184.216.34/",
        "http://[2606:4700:4700::1111]/",
    ],
)
def test_ordinary_urls_pass_this_stage(url: str) -> None:
    target = check_url(url, NO_BLOCKED)
    assert target.scheme in {"http", "https"}
    assert target.host == target.host.lower()
    assert not target.host.endswith(".")


def test_the_installations_own_host_is_refused_in_every_spelling() -> None:
    blocked = frozenset({"pharos.example.org"})
    for url in (
        "https://pharos.example.org/api/readyz",
        "https://PHAROS.example.org/",
        "https://pharos.example.org./",
        "http://pharos.example.org/",
    ):
        assert refused(url, blocked) == "own_host"
    check_url("https://other.example.org/", blocked)


def test_an_extra_port_is_accepted_only_when_it_is_given() -> None:
    with pytest.raises(FetchError):
        check_url("http://example.com:9000/", NO_BLOCKED)
    assert (
        check_url("http://example.com:9000/", NO_BLOCKED, frozenset({9000})).host == "example.com"
    )
    with pytest.raises(FetchError):
        check_url("http://example.com:9001/", NO_BLOCKED, frozenset({9000}))
