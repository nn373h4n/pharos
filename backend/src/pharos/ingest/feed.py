"""Parsing an RSS or Atom document that anybody on the internet may have written (ADR-0006).

The order matters:

1. The size is already capped by `SafeHttpClient` (2 MiB, raw bytes, no decompression).
2. The bytes are read once with `defusedxml`: a document type declaration, an entity or an external
   reference is refused outright (billion laughs, XXE), and so is a document with too many elements
   or too deep a nesting. A legitimate feed has none of these. Some real feeds are not well-formed
   XML; for those the strict read fails with an ordinary parse error, and the bytes are accepted for
   the tolerant parser only if a plain scan finds no declaration or entity in any text encoding.
3. `feedparser` parses the bytes and only the bytes, handed over as a file-like object. Handing it a
   `str` or `bytes` would make it treat the content as a URL or a file name and open it.
4. Every field that leaves is reduced to bounded plain text (`ingest.text`) and links are checked
   (`ingest.links`). At most `MAX_ENTRIES` entries are looked at.
"""

import calendar
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import defusedxml
import feedparser
from defusedxml import ElementTree as SafeElementTree
from defusedxml.ElementTree import ParseError

from pharos.ingest.links import safe_link
from pharos.ingest.text import clean_identifier, clean_text

MAX_ENTRIES = 200
MAX_ELEMENTS = 100_000
MAX_DEPTH = 64
TITLE_LIMIT = 300
EXCERPT_LIMIT = 500
EXTERNAL_ID_LIMIT = 512

_EARLIEST = datetime(1990, 1, 1, tzinfo=UTC)
_FUTURE_SLACK = timedelta(days=1)
# A declaration or an entity, in the encodings an attacker could choose to hide one.
_DECLARATION = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


class FeedError(Exception):
    """The document is not a usable feed. `code` is a short, fixed word (never document text)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RawEntry:
    external_id: str
    title: str
    excerpt: str
    url: str
    published_at: datetime | None
    content_sha256: str


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    entries: tuple[RawEntry, ...]
    total: int  # entries the document holds (up to the cap); the difference was skipped
    skipped: int


def _decodings(body: bytes) -> list[str]:
    """The text of `body` under the encodings a declaration could be hidden in."""
    texts = [body.decode("latin-1")]  # sees ASCII-compatible encodings, never fails
    for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
        try:
            texts.append(body.decode(encoding, errors="ignore"))
        except (UnicodeError, LookupError):  # pragma: no cover - errors="ignore" does not raise
            continue
    return texts


def check_xml(body: bytes) -> None:
    """Refuse documents that use XML features a feed never needs; see the module docstring."""
    elements = depth = 0
    try:
        for event, element in SafeElementTree.iterparse(
            io.BytesIO(body), events=("start", "end"), forbid_dtd=True
        ):
            if event == "start":
                elements += 1
                depth += 1
                if elements > MAX_ELEMENTS or depth > MAX_DEPTH:
                    raise FeedError("too_complex")
            else:
                depth -= 1
                element.clear()  # the tree is not kept: a big document costs time, not memory
    except defusedxml.DefusedXmlException:
        raise FeedError(
            "bad_content"
        ) from None  # a declaration, an entity or an external reference
    except ParseError:
        # Not well-formed XML. The tolerant parser may still read it, but only if no declaration or
        # entity is hiding anywhere in the bytes.
        if any(_DECLARATION.search(text) for text in _decodings(body)):
            raise FeedError("bad_content") from None
        if body.count(b"<") > MAX_ELEMENTS * 4:
            raise FeedError("too_complex") from None
    except (ValueError, LookupError):  # an unknown or inconsistent encoding declaration
        raise FeedError("bad_content") from None


def _published(entry: Any, now: datetime) -> datetime | None:
    # dict.get on purpose: FeedParserDict.get maps one key onto the other (and warns about it).
    stamp = dict.get(entry, "published_parsed") or dict.get(entry, "updated_parsed")
    if not stamp:
        return None
    try:
        moment = datetime.fromtimestamp(calendar.timegm(stamp), tz=UTC)
    except (OverflowError, OSError, ValueError, TypeError):
        return None
    if moment < _EARLIEST or moment > now + _FUTURE_SLACK:
        return None  # a clock error or a trick to stay at the top of a list
    return moment


def _summary(entry: Any) -> str:
    summary = entry.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary
    content = entry.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        value = content[0].get("value")
        if isinstance(value, str):
            return value
    return ""


def parse_feed(body: bytes, now: datetime) -> ParsedFeed:
    """Entries of the feed in `body`, cleaned and bounded. Raises FeedError for anything else."""
    if not isinstance(body, bytes):  # a str would be read by feedparser as a URL or a path
        raise TypeError("parse_feed takes bytes")
    if not body.strip():
        raise FeedError("bad_content")
    check_xml(body)
    # A file-like object, never the bytes themselves: feedparser opens anything without a `read`
    # method as a file name or a URL, so a body that is exactly "/dev/zero" would be read from disk.
    parsed = feedparser.parse(io.BytesIO(body))
    entries = parsed.get("entries") or []
    if not entries and (parsed.get("bozo") or not parsed.get("version")):
        raise FeedError("bad_content")  # not a feed at all (an HTML page, text, an error page)
    feed_link = (parsed.get("feed") or {}).get("link")
    base = feed_link if isinstance(feed_link, str) else None

    result: list[RawEntry] = []
    looked_at = skipped = 0
    for entry in entries[:MAX_ENTRIES]:
        looked_at += 1
        url = safe_link(entry.get("link"), base)
        title = clean_text(entry.get("title"), TITLE_LIMIT)
        if url is None or not title:
            skipped += 1
            continue
        excerpt = clean_text(_summary(entry), EXCERPT_LIMIT)
        published = _published(entry, now)
        identifier = clean_identifier(entry.get("id"), EXTERNAL_ID_LIMIT) or url[:EXTERNAL_ID_LIMIT]
        digest = hashlib.sha256(f"{title}\n{excerpt}\n{url}".encode()).hexdigest()
        result.append(RawEntry(identifier, title, excerpt, url, published, digest))
    return ParsedFeed(tuple(result), looked_at, skipped)
