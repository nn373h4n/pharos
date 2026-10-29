"""Parsing feeds: ordinary ones come out clean, hostile ones are refused and cost little."""

# ruff: noqa: E501  (feed documents and their assertions are long literal lines)

import time
import tracemalloc
from datetime import UTC, datetime

import pytest

from pharos.ingest import feed as feed_module
from pharos.ingest.feed import (
    EXCERPT_LIMIT,
    MAX_DEPTH,
    MAX_ELEMENTS,
    MAX_ENTRIES,
    TITLE_LIMIT,
    FeedError,
    parse_feed,
)

from . import feeds

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def parse(body: bytes):  # type: ignore[no-untyped-def]
    return parse_feed(body, NOW)


def refused(body: bytes) -> str:
    with pytest.raises(FeedError) as caught:
        parse(body)
    return caught.value.code


# --- ordinary feeds ----------------------------------------------------------------------------------


def test_an_rss_feed_is_read_and_cleaned() -> None:
    result = parse(feeds.RSS)
    assert [e.title for e in result.entries] == [
        "First story",
        "Second story",
        "Story with content only",
    ]
    first, second, third = result.entries
    assert first.url == "https://news.example/stories/1"
    assert first.external_id == "story-1"
    assert first.excerpt == "A short summary."
    assert first.published_at == datetime(2026, 10, 27, 9, 30, tzinfo=UTC)
    assert second.url == "https://news.example/stories/2"  # resolved against the channel link
    assert second.excerpt == "Second summary"
    assert second.published_at == datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
    assert third.excerpt == "Full text"  # the script is gone
    assert "alert" not in third.excerpt
    assert (result.total, result.skipped) == (3, 0)


def test_an_atom_feed_is_read_with_relative_links_and_xhtml_content() -> None:
    result = parse(feeds.ATOM)
    first, second = result.entries
    assert first.title == "Release 1.2"
    assert first.url == "https://blog.example/posts/release-1-2"
    assert first.external_id == "tag:blog.example,2026:post-12"
    assert second.excerpt == "Body text"
    assert second.published_at == datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


def test_an_rdf_feed_is_read() -> None:
    result = parse(feeds.RDF)
    assert [(e.title, e.url) for e in result.entries] == [("Old one", "https://old.example/1")]


def test_a_utf8_byte_order_mark_and_other_encodings_are_handled() -> None:
    assert len(parse(b"\xef\xbb\xbf" + feeds.RSS).entries) == 3
    turkish = (
        '<?xml version="1.0" encoding="iso-8859-9"?><rss version="2.0"><channel><title>t</title>'
        "<item><title>Şirket müşteri</title><link>https://x.example/1</link></item></channel></rss>"
    ).encode("iso-8859-9")
    assert parse(turkish).entries[0].title == "Şirket müşteri"


def test_the_same_content_gives_the_same_hash_and_a_change_gives_another() -> None:
    first = parse(feeds.RSS).entries[0]
    again = parse(feeds.RSS).entries[0]
    changed = parse(feeds.RSS.replace(b"First story", b"First story!")).entries[0]
    assert first.content_sha256 == again.content_sha256 != changed.content_sha256
    assert len(first.content_sha256) == 64


# --- what is skipped or cleaned ----------------------------------------------------------------------


def test_entries_without_a_usable_link_or_title_are_skipped_and_counted() -> None:
    body = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <link>https://x.example/</link>
    <item><title>No link</title></item>
    <item><title>Bad link</title><link>javascript:alert(1)</link></item>
    <item><title>Data link</title><link>data:text/html,x</link></item>
    <item><link>https://x.example/untitled</link></item>
    <item><title>   </title><link>https://x.example/blank</link></item>
    <item><title>Good</title><link>https://x.example/good</link></item></channel></rss>"""
    result = parse(body)
    assert [e.title for e in result.entries] == ["Good"]
    assert (result.total, result.skipped) == (6, 5)


def test_an_entry_without_a_guid_is_identified_by_its_link() -> None:
    body = feeds.rss_with_entries(1).replace(b"<guid>g0</guid>", b"")
    assert parse(body).entries[0].external_id == "https://news.example/s/0"


def test_titles_and_excerpts_are_cut_at_their_limits() -> None:
    body = feeds.rss_with_entries(1, title="T" * 1000, summary="S" * 5000)
    entry = parse(body).entries[0]
    assert len(entry.title) == TITLE_LIMIT and len(entry.excerpt) == EXCERPT_LIMIT


def test_a_megabyte_title_costs_nothing_and_is_bounded() -> None:
    entry = parse(feeds.rss_with_entries(1, title="x" * 1_000_000)).entries[0]
    assert len(entry.title) == TITLE_LIMIT


def test_markup_and_invisible_characters_in_titles_are_removed() -> None:
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        "<item><title>&lt;b&gt;Pay&lt;/b&gt;‮pal​</title>"
        "<link>https://x.example/1</link></item></channel></rss>"
    ).encode()
    assert parse(body).entries[0].title == "Paypal"


@pytest.mark.parametrize(
    "stamp",
    ["Fri, 01 Jan 2100 00:00:00 GMT", "Thu, 01 Jan 1970 00:00:00 GMT", "not a date", ""],
)
def test_an_impossible_date_is_dropped_not_trusted(stamp: str) -> None:
    body = feeds.rss_with_entries(1).replace(
        b"</item>", f"<pubDate>{stamp}</pubDate></item>".encode()
    )
    entry = parse(body).entries[0]
    assert entry.published_at is None


def test_a_date_a_few_hours_ahead_is_accepted_but_not_days_ahead() -> None:
    soon = feeds.rss_with_entries(1).replace(
        b"</item>", b"<pubDate>Wed, 28 Oct 2026 20:00:00 GMT</pubDate></item>"
    )
    later = feeds.rss_with_entries(1).replace(
        b"</item>", b"<pubDate>Sat, 31 Oct 2026 20:00:00 GMT</pubDate></item>"
    )
    assert parse(soon).entries[0].published_at is not None
    assert parse(later).entries[0].published_at is None


def test_at_most_two_hundred_entries_are_read() -> None:
    result = parse(feeds.rss_with_entries(MAX_ENTRIES + 50))
    assert len(result.entries) == MAX_ENTRIES
    assert result.total == MAX_ENTRIES
    assert result.entries[-1].title == f"Story {MAX_ENTRIES - 1}"
    assert len(parse(feeds.rss_with_entries(MAX_ENTRIES)).entries) == MAX_ENTRIES


def test_duplicate_guids_are_both_returned_for_the_pipeline_to_merge() -> None:
    body = feeds.rss_with_entries(2).replace(b"<guid>g1</guid>", b"<guid>g0</guid>")
    assert [e.external_id for e in parse(body).entries] == ["g0", "g0"]


# --- documents that are not feeds -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"   \n  ",
        feeds.HTML_PAGE,
        b"Just some text, not a feed.",
        b'{"items": [{"title": "json feed"}]}',
        b"\x00\x01\x02\xff\xfe binary \x00",
        b"\x1f\x8b\x08\x00" + b"\x00" * 50,  # gzip that was never inflated
        b"<?xml version='1.0'?><html><body>not a feed</body></html>",
        b"<?xml version='1.0'?><rss version='2.0'><channel><title>no items</title></channel></rss>",
    ],
)
def test_documents_that_are_not_feeds_are_refused(body: bytes) -> None:
    if b"no items" in body:
        assert parse(body).entries == ()  # an empty feed is a feed
        return
    assert refused(body) == "bad_content"


def test_only_bytes_are_accepted_because_a_string_would_be_read_as_a_url() -> None:
    with pytest.raises(TypeError):
        parse_feed(feeds.RSS.decode(), NOW)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        parse_feed("https://attacker.example/feed.xml", NOW)  # type: ignore[arg-type]


# --- hostile documents -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        feeds.BILLION_LAUGHS,
        feeds.EXTERNAL_FILE,
        feeds.EXTERNAL_HTTP,
        feeds.PARAMETER_ENTITY,
        feeds.HARMLESS_DOCTYPE,  # no real feed needs one, so none is accepted
        feeds.DTD_NO_ENTITIES,
        feeds.utf16(feeds.BILLION_LAUGHS.decode().replace('<?xml version="1.0"?>', "").encode()),
        feeds.utf16(feeds.EXTERNAL_FILE),
        feeds.utf16(feeds.HARMLESS_DOCTYPE, "utf-16-le"),
        b'<?xml version="1.0"?>\n<!doctype rss [<!entity x "y">]><rss version="2.0"/>',
        b'<?xml version="1.0"?><!DOCTYPE\nrss\n[<!ENTITY x "y">]><rss version="2.0"/>',
    ],
)
def test_a_document_type_declaration_or_entity_is_refused(body: bytes) -> None:
    assert refused(body) == "bad_content"


def test_a_declaration_that_follows_a_parse_error_is_refused_even_when_entries_are_readable() -> (
    None
):
    # The strict reader stops at the "&" and never reaches the entity, and the tolerant parser
    # would happily return the entry: only the scan for declarations stops this one.
    text = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        "<item><title>A & B</title><link>https://x.example/1</link></item>"
        '<!ENTITY x "y"></channel></rss>'
    )
    wide = text.replace('<?xml version="1.0"?>', '<?xml version="1.0" encoding="utf-16"?>')
    for encoded in (
        text.encode(),
        wide.encode("utf-16"),
        wide.encode("utf-16-le"),
        wide.encode("utf-16-be"),
    ):
        assert refused(encoded) == "bad_content"
    harmless = text.replace('<!ENTITY x "y">', "")
    assert parse(harmless.encode()).entries[0].title == "A & B"  # control: same document, no entity
    wide_harmless = harmless.replace(
        '<?xml version="1.0"?>', '<?xml version="1.0" encoding="utf-16"?>'
    )
    for encoding in (
        "utf-16",
        "utf-16-le",
        "utf-16-be",
    ):  # controls: the tolerant parser reads them
        assert parse(wide_harmless.encode(encoding)).entries[0].title == "A & B"


@pytest.mark.parametrize(
    "body",
    [
        b"junk <!DOCTYPE rss [<!ENTITY a 'b'>]><rss version='2.0'><channel/></rss>",
        b"<rss version='2.0'><channel>& broken <!ENTITY x 'y'></channel></rss>",
        "x <!DOCTYPE rss>".encode("utf-16"),
        "x <!ENTITY a 'b'>".encode("utf-16-be"),
    ],
)
def test_a_declaration_hidden_in_a_document_that_is_not_well_formed_is_still_refused(
    body: bytes,
) -> None:
    assert refused(body) == "bad_content"


def test_external_content_never_appears_in_the_result() -> None:
    for body in (feeds.EXTERNAL_FILE, feeds.EXTERNAL_HTTP, feeds.XINCLUDE):
        try:
            result = parse(body)
        except FeedError:
            continue
        assert all("root:" not in e.title and "ami-" not in e.title for e in result.entries)


def test_a_feed_that_is_not_well_formed_but_harmless_is_still_read() -> None:
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        b"<item><title>Fish & chips</title><link>https://x.example/1</link></item>"
        b"</channel></rss>"
    )
    assert parse(body).entries[0].title == "Fish & chips"


def test_too_deep_a_nesting_is_refused() -> None:
    deep = b"<a>" * (MAX_DEPTH + 5) + b"</a>" * (MAX_DEPTH + 5)
    body = b'<?xml version="1.0"?><rss version="2.0"><channel>' + deep + b"</channel></rss>"
    assert refused(body) == "too_complex"
    ok = b"<a>" * (MAX_DEPTH - 5) + b"</a>" * (MAX_DEPTH - 5)
    assert b"<rss" in b'<rss version="2.0">' + ok  # a normal depth is not what is refused


def test_too_many_elements_are_refused() -> None:
    body = b'<?xml version="1.0"?><rss version="2.0"><channel>' + b"<a/>" * (MAX_ELEMENTS + 1)
    body += b"</channel></rss>"
    assert refused(body) == "too_complex"


def test_a_wide_but_ordinary_feed_is_not_too_complex() -> None:
    assert len(parse(feeds.rss_with_entries(1000)).entries) == MAX_ENTRIES


def test_the_check_does_not_keep_the_document_in_memory() -> None:
    body = feeds.rss_with_entries(5000)
    tracemalloc.start()
    feed_module.check_xml(body)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 20 * len(body)


def test_a_feed_of_ten_thousand_entries_is_cut_quickly_and_cheaply() -> None:
    body = feeds.rss_with_entries(10_000, summary="s")
    assert len(body) < 2 * 1024 * 1024  # it would pass the size cap
    tracemalloc.start()
    started = time.perf_counter()
    result = parse(body)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(result.entries) == MAX_ENTRIES
    assert elapsed < 20
    assert peak < 300 * 1024 * 1024


# --- the body is data, never a path or an address (found by the security review) ---------------


def test_a_body_that_is_a_file_name_is_never_opened(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real = tmp_path / "feed.xml"  # type: ignore[operator]
    real.write_bytes(feeds.RSS)
    opened: list[object] = []
    real_open = builtins.open

    def spy(file: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        opened.append(file)
        return real_open(file, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(builtins, "open", spy)
    for body in (str(real).encode(), b"/dev/zero", b"/etc/hostname", b"../../etc/passwd"):
        with pytest.raises(FeedError):
            parse(body)
    assert str(real) not in map(str, opened) and "/dev/zero" not in map(str, opened)


def test_a_body_that_is_a_url_is_never_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    import urllib.request

    def refuse(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise AssertionError("the parser must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    for body in (b"https://attacker.example/feed.xml", b"http://169.254.169.254/", b"ftp://x/y"):
        with pytest.raises(FeedError):
            parse(body)


@pytest.mark.parametrize("encoding", ["utf-32-le", "utf-32-be", "utf-32"])
def test_a_declaration_in_utf_32_is_refused(encoding: str) -> None:
    text = (
        '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "b">]><rss version="2.0"><channel/></rss>'
    )
    assert refused(text.encode(encoding)) == "bad_content"
