"""Only absolute http(s) links survive; everything a hostile feed might try is refused."""

import pytest

from pharos.ingest.links import MAX_URL_LENGTH, safe_link

OK = [
    "https://example.org/a",
    "http://example.org/a?x=1&y=2#frag",
    "HTTPS://EXAMPLE.ORG/A",
    "https://example.org:8443/p",
    "https://sub.example.org./x",
    "http://[2606:4700:4700::1111]/x",
    "https://bücher.example/ärger",
    "https://example.org/a%20b",
    "https://xn--bcher-kva.example/",
]
HOSTILE = [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    " javascript:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "java\x00script:alert(1)",
    "vbscript:msgbox(1)",
    "data:text/html,<script>alert(1)</script>",
    "data:text/html;base64,PHNjcmlwdD4=",
    "file:///etc/passwd",
    "ftp://example.org/x",
    "gopher://example.org/",
    "ws://example.org/",
    "blob:https://example.org/1",
    "about:blank",
    "mailto:a@example.org",
    "tel:+123",
    "//example.org/x",  # scheme-relative with no base
    "/relative/path",
    "relative/path",
    "?query-only",
    "#fragment-only",
    "https://",
    "http:///path",
    "https://user:pass@example.org/",
    "https://user@example.org/",
    "https://example.org@evil.example/",
    "https://example.org/a b",
    "https://example.org\\.evil.example/",
    "https://example.org/a\\b",
    "https://example.org/a\tb",
    "https://example.org/a\nb",
    "https://example.org/a\r\nSet-Cookie: x=1",
    'https://example.org/"onmouseover="alert(1)',
    "https://example.org/<script>",
    "https://example.org/a\x00b",
    "https://example.org/a\x7fb",
    "https://example.org:99999/",
    "https://example.org:abc/",
    "https://exa mple.org/",
    "https://" + "a" * MAX_URL_LENGTH + ".example/",
    "",
    "   ",
]


@pytest.mark.parametrize("url", OK)
def test_ordinary_links_pass_unchanged(url: str) -> None:
    assert safe_link(url) == url


@pytest.mark.parametrize("url", HOSTILE)
def test_hostile_or_broken_links_are_refused(url: str) -> None:
    assert safe_link(url) is None


@pytest.mark.parametrize("value", [None, 5, b"https://example.org/", ["https://example.org/"], {}])
def test_things_that_are_not_text_are_refused(value: object) -> None:
    assert safe_link(value) is None


def test_the_length_limit_is_exact() -> None:
    prefix = "https://example.org/"
    assert safe_link(prefix + "a" * (MAX_URL_LENGTH - len(prefix))) is not None
    assert safe_link(prefix + "a" * (MAX_URL_LENGTH - len(prefix) + 1)) is None


@pytest.mark.parametrize(
    ("link", "base", "expected"),
    [
        ("/post/1", "https://example.org/blog/", "https://example.org/post/1"),
        ("post/1", "https://example.org/blog/", "https://example.org/blog/post/1"),
        ("//cdn.example.org/x", "https://example.org/", "https://cdn.example.org/x"),
        ("?page=2", "https://example.org/blog", "https://example.org/blog?page=2"),
        ("https://other.example/x", "https://example.org/", "https://other.example/x"),
    ],
)
def test_a_relative_link_is_resolved_against_the_feed(link: str, base: str, expected: str) -> None:
    assert safe_link(link, base) == expected


@pytest.mark.parametrize(
    ("link", "base"),
    [
        ("/x", "javascript:alert(1)"),  # an unsafe base is not used
        ("/x", None),
        ("javascript:alert(1)", "https://example.org/"),
        ("/x", "https://user:pw@example.org/"),
    ],
)
def test_a_relative_link_without_a_safe_base_is_refused(link: str, base: str | None) -> None:
    assert safe_link(link, base) is None


def test_a_link_cannot_escape_the_checks_by_resolving_into_something_unsafe() -> None:
    assert safe_link("javascript:alert(1)", "https://example.org/") is None
    assert safe_link("data:text/html,x", "https://example.org/") is None


@pytest.mark.parametrize("link", ["po\tst/1", "post/\n1", "pos\x00t", "po st", 'po"st', "po<st"])
def test_a_relative_link_with_white_space_or_control_characters_is_refused_not_repaired(
    link: str,
) -> None:
    # urljoin would silently drop a tab or a newline and hand back a clean-looking address.
    assert safe_link(link, "https://example.org/blog/") is None
    assert safe_link("post/1", "https://example.org/blog/") == "https://example.org/blog/post/1"
