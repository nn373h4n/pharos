"""The starter catalogue file and its checks (no database, no network)."""

import json
from pathlib import Path
from typing import Any

import pytest

from pharos.sources import catalogue


def document(*entries: dict[str, Any], version: int = 1) -> bytes:
    return json.dumps({"version": version, "sources": list(entries)}).encode()


def feed(
    name: str = "Industry news", url: str = "https://news.example.org/feed.xml"
) -> dict[str, Any]:
    return {
        "type": "rss",
        "name": name,
        "config": {"url": url},
        "interval_seconds": 3600,
        "note": "A public feed for tests.",
    }


def test_the_catalogue_that_ships_is_valid() -> None:
    entries = catalogue.load()
    assert len(entries) <= catalogue.MAX_ENTRIES
    assert all(entry.config and entry.note for entry in entries)
    # Only public web addresses: nothing here may be fetched over plain HTTP.
    for entry in entries:
        if entry.type == "rss":
            assert entry.config["url"].startswith("https://")


def test_a_valid_file_gives_checked_entries() -> None:
    github = {
        "type": "github",
        "name": "Widget releases",
        "config": {"repo": "acme/widget"},
        "interval_seconds": 21600,
        "note": "Releases of a public repository.",
    }
    entries = catalogue.parse(document(feed(), github))
    assert [entry.type for entry in entries] == ["rss", "github"]
    assert all(entry.dedupe_key for entry in entries)
    assert entries[0].interval_seconds == 3600


def test_an_empty_catalogue_is_valid() -> None:
    assert catalogue.parse(document()) == []


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"not json", "not valid"),
        (b"[]", "not valid"),
        (document(version=2), "unsupported catalogue version"),
        (json.dumps({"version": 1}).encode(), "not valid"),
        (json.dumps({"version": 1, "sources": [], "extra": 1}).encode(), "not valid"),
        (b"x" * (catalogue.MAX_FILE_BYTES + 1), "too large"),
    ],
)
def test_a_broken_file_is_refused(raw: bytes, message: str) -> None:
    with pytest.raises(catalogue.CatalogueError, match=message):
        catalogue.parse(raw)


def test_an_entry_cannot_switch_itself_on() -> None:
    with pytest.raises(catalogue.CatalogueError, match=r"entry 1.*enabled"):
        catalogue.parse(document({**feed(), "enabled": True}))


@pytest.mark.parametrize(
    "change",
    [
        {"type": "ftp"},
        {"name": ""},
        {"name": "<b>bold</b>"},
        {"name": "x" * 201},
        {"name": "zero​width"},
        {"interval_seconds": 10},
        {"interval_seconds": 10**9},
        {"interval_seconds": "3600"},
        {"note": ""},
        {"note": "<script>alert(1)</script>"},
        {"note": "n" * (catalogue.NOTE_LIMIT + 1)},
        {"config": {}},
        {"config": {"url": "http://news.example.org/feed.xml"}},
        {"config": {"url": "https://localhost/feed.xml"}},
        {"config": {"url": "https://intranet/feed.xml"}},
        {"config": {"url": "https://127.0.0.1/feed.xml"}},
        {"config": {"url": "https://user:pass@news.example.org/feed.xml"}},
    ],
)
def test_an_entry_that_the_api_would_refuse_is_refused(change: dict[str, Any]) -> None:
    with pytest.raises(catalogue.CatalogueError, match="entry 1"):
        catalogue.parse(document({**feed(), **change}))


def test_the_same_source_twice_is_refused_even_when_spelled_differently() -> None:
    with pytest.raises(catalogue.CatalogueError, match="listed twice"):
        catalogue.parse(
            document(
                feed("One", "https://news.example.org/feed.xml"),
                feed("Two", "https://NEWS.example.org/feed.xml"),
            )
        )


def test_the_same_name_twice_is_refused() -> None:
    with pytest.raises(catalogue.CatalogueError, match="used twice"):
        catalogue.parse(
            document(
                feed("Same", "https://a.example.org/feed.xml"),
                feed("same", "https://b.example.org/feed.xml"),
            )
        )


def test_too_many_entries_are_refused() -> None:
    entries = [feed(f"Source {n}", f"https://host{n}.example.org/feed.xml") for n in range(101)]
    with pytest.raises(catalogue.CatalogueError, match="not valid"):
        catalogue.parse(document(*entries))


def test_the_host_of_this_installation_can_be_blocked() -> None:
    with pytest.raises(catalogue.CatalogueError, match="config refused"):
        catalogue.parse(
            document(feed(url="https://pharos.example.org/feed.xml")),
            blocked_hosts=("pharos.example.org",),
        )


def test_a_missing_file_is_told_without_a_traceback(tmp_path: Path) -> None:
    with pytest.raises(catalogue.CatalogueError, match="cannot read"):
        catalogue.load(tmp_path / "nope.json")


def test_a_file_is_read_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "mine.json"
    path.write_bytes(document(feed()))
    assert [entry.name for entry in catalogue.load(path)] == ["Industry news"]


def test_a_deeply_nested_file_is_refused_without_a_traceback() -> None:
    raw = b'{"version":1,"sources":' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    with pytest.raises(catalogue.CatalogueError, match="not valid"):
        catalogue.parse(raw)


def test_keys_from_the_file_are_shown_quoted_not_raw() -> None:
    with pytest.raises(catalogue.CatalogueError) as caught:
        catalogue.parse(document({**feed(), "\x1b]0;evil\x07": 1}))
    assert "\x1b" not in str(caught.value)
    assert "\x07" not in str(caught.value)
