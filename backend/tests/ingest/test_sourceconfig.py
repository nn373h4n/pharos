"""What a source's configuration may say (M3-10): rules, keys, early refusal of internal hosts."""

# ruff: noqa: RUF001  (look-alike characters are what several of these tests are about)

import subprocess
import sys

import pytest

from pharos.ingest.connectors import build_connectors
from pharos.ingest.sourceconfig import (
    ConfigError,
    normalized_feed_url,
    refuse_internal_host,
    validate,
)

CONNECTORS = build_connectors()


def code_of(source_type: str, raw: dict, **options: object) -> str:
    with pytest.raises(ConfigError) as caught:
        validate(source_type, raw, **options)  # type: ignore[arg-type]
    return caught.value.code


# --- the same rules as the connectors, because they are the same code ---


@pytest.mark.parametrize(
    ("source_type", "raw"),
    [
        ("rss", {"url": "https://news.example/feed.xml"}),
        ("github", {"repo": "acme/widget"}),
        ("hackernews", {"query": " Finance close ", "min_points": 5}),
        ("hackernews", {"query": "sap"}),
    ],
)
def test_the_connector_and_the_api_agree_on_configuration_and_key(
    source_type: str, raw: dict
) -> None:
    connector = CONNECTORS[source_type]
    checked = validate(source_type, raw)
    assert checked.config == connector.validate_config(raw)
    assert checked.dedupe_key == connector.dedupe_key(checked.config)


@pytest.mark.parametrize(
    ("source_type", "raw", "code"),
    [
        ("rss", {}, "url_required"),
        ("rss", {"url": "javascript:alert(1)"}, "url_invalid"),
        ("rss", {"url": "https://news.example/", "x": 1}, "unknown_setting"),
        ("github", {"repo": "acme/.."}, "repo_invalid"),
        ("github", {}, "repo_required"),
        ("hackernews", {"query": "<b>x</b>"}, "query_invalid"),
        ("hackernews", {"query": "sap", "min_points": True}, "min_points_invalid"),
        ("ftp", {"url": "https://news.example/"}, "unknown_type"),
        ("", {}, "unknown_type"),
    ],
)
def test_invalid_configurations_have_fixed_codes(source_type: str, raw: dict, code: str) -> None:
    assert code_of(source_type, raw) == code


def test_two_spellings_of_one_address_have_one_key() -> None:
    a = validate("rss", {"url": "https://News.Example:443/feed.xml#top"}).dedupe_key
    assert a == validate("rss", {"url": "https://news.example/feed.xml"}).dedupe_key
    assert a != validate("rss", {"url": "https://news.example/other.xml"}).dedupe_key


def test_the_feed_address_spelling_is_still_importable_from_the_connector() -> None:
    from pharos.ingest.connectors.rss import normalized_feed_url as from_connector

    assert from_connector is normalized_feed_url


# --- an internal address is refused when it is typed, not only when fetched ---


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/feed",
        "http://127.1/feed",
        "http://2130706433/feed",
        "http://0x7f000001/feed",
        "http://0177.0.0.1/feed",
        "http://10.0.0.5/feed",
        "http://192.168.1.1/feed",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/feed",
        "http://[::ffff:7f00:1]/feed",
        "http://[2001:db8::1]/feed",
        "http://localhost/feed",
        "http://LOCALHOST./feed",
        "http://app.localhost/feed",
        "http://redis/feed",
        "http://postgres:5432/x",
        "http://api:8000/x",
        "http://printer.local/feed",
        "http://wiki.internal/feed",
        "http://nas.lan/feed",
        "http://router.home.arpa/feed",
        "http://host.localdomain/feed",
        "https://203.0.113.9/feed",
        "https://1.1.1.1./feed",
    ],
)
def test_an_address_that_can_only_be_internal_is_refused(url: str) -> None:
    assert code_of("rss", {"url": url}) == "url_not_allowed"


@pytest.mark.parametrize(
    "url",
    [
        "https://news.example.org/feed.xml",
        "http://blog.example.com:8080/rss",
        "https://a1.example/feed",
        "https://1password.com/feed",
        "https://xn--e1afmkfd.xn--p1ai/feed",
        "https://my-site.co.uk/atom",
    ],
)
def test_ordinary_public_names_pass(url: str) -> None:
    assert validate("rss", {"url": url}).config == {"url": url}


def test_the_own_host_of_the_installation_is_refused_in_any_spelling() -> None:
    blocked = ["pharos.example.org"]
    for url in (
        "https://pharos.example.org/feed",
        "https://PHAROS.Example.Org./feed",
        "http://pharos.example.org:8080/feed",
    ):
        assert code_of("rss", {"url": url}, blocked_hosts=blocked) == "url_not_allowed"
    assert validate("rss", {"url": "https://other.example.org/feed"}, blocked_hosts=blocked)


def test_only_feed_addresses_are_checked_for_internal_hosts() -> None:
    # GitHub and Hacker News always go to a fixed host: their settings hold no address.
    assert validate("github", {"repo": "localhost/redis"}).config == {"repo": "localhost/redis"}
    assert validate("hackernews", {"query": "localhost"}).config["query"] == "localhost"


def test_refuse_internal_host_alone() -> None:
    refuse_internal_host("https://news.example.org/feed")
    with pytest.raises(ConfigError):
        refuse_internal_host("https://127.0.0.1/feed")
    with pytest.raises(ConfigError):
        refuse_internal_host("not a url")


# --- the module has no network in it ---


@pytest.mark.parametrize(
    "module",
    [
        "pharos.ingest.sourceconfig",
        "pharos.sources.service",
        "pharos.sources.router",
        "pharos.items.service",
        "pharos.items.router",
    ],
)
def test_importing_it_never_loads_an_http_client(module: str) -> None:
    code = (
        "import sys; import " + module + "; "
        "bad = [m for m in ('httpx', 'httpcore', 'pharos.net.safe_http', 'pharos.ingest.fetcher', "
        "'pharos.ingest.connectors', 'pharos.workers', 'urllib.request') if m in sys.modules]; "
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(  # noqa: S603  (our own interpreter and a fixed program)
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- the host as a connection sees it (found by the security review of M3-10a) ---


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254。/latest",  # ideographic full stop
        "http://169.254.169.254．/latest",  # fullwidth full stop
        "http://169.254.169.254｡/latest",  # halfwidth ideographic full stop
        "http://127．0．0．1/",
        "http://127。0。0。1/",
        "http://１２７.0.0.1/",  # fullwidth digits
        "http://metadata.google.internal。/",
        "http://printer。local/",
        "http://localhost。/",
    ],
)
def test_other_kinds_of_dot_do_not_hide_an_internal_address(url: str) -> None:
    assert code_of("rss", {"url": url}) == "url_not_allowed"


@pytest.mark.parametrize("separator", ["。", "．", "｡"])
def test_the_own_host_is_found_whatever_dot_ends_or_separates_it(separator: str) -> None:
    blocked = ["pharos.example.org"]
    for url in (
        f"https://pharos.example.org{separator}/feed",
        f"https://pharos{separator}example{separator}org/feed",
    ):
        assert code_of("rss", {"url": url}, blocked_hosts=blocked) == "url_not_allowed"


@pytest.mark.parametrize("url", ["http://a..b.example/", "http://" + "x" * 70 + ".example/"])
def test_a_host_that_cannot_be_written_as_ascii_is_refused(url: str) -> None:
    assert code_of("rss", {"url": url}) == "url_not_allowed"


def test_one_feed_in_several_spellings_has_one_key() -> None:
    plain = validate("rss", {"url": "https://news.example.org/feed"}).dedupe_key
    for url in (
        "https://news.example.org。/feed",
        "https://NEWS．Example｡ORG/feed",
        "https://news.example.org./feed",
    ):
        assert validate("rss", {"url": url}).dedupe_key == plain, url


def test_an_international_name_and_its_punycode_are_one_source() -> None:
    unicode_name = validate("rss", {"url": "https://пример.example.org/feed"})
    ascii_name = validate("rss", {"url": "https://xn--e1afmkfd.example.org/feed"})
    assert unicode_name.dedupe_key == ascii_name.dedupe_key


def test_ascii_host_directly() -> None:
    from pharos.ingest.sourceconfig import ascii_host

    assert ascii_host("News.Example.ORG.") == "news.example.org"
    assert ascii_host("a。b") == "a.b"
    assert ascii_host("2001:db8::1") == "2001:db8::1"
    assert ascii_host("a..b") is None
    assert ascii_host("") == ""
