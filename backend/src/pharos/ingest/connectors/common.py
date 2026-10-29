"""What the connectors that read JSON (GitHub, Hacker News) share.

A JSON answer is as untrusted as a feed: it may be deeply nested, full of the wrong types, carry
huge numbers, markup, invisible characters or a `javascript:` link. Everything that leaves this
module has gone through the same cleaning as a feed entry (`ingest.text`, `ingest.links`) and has
the same bounds, so the pipeline cannot tell where an entry came from.
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pharos.ingest.connectors.base import ConnectorError
from pharos.ingest.feed import EXCERPT_LIMIT, EXTERNAL_ID_LIMIT, TITLE_LIMIT, RawEntry
from pharos.ingest.links import safe_link
from pharos.ingest.text import clean_identifier, clean_text

# What a server may give us to send back: visible ASCII only, and short (the database allows 512
# for an ETag and 128 for a date). Anything else is dropped rather than echoed into a request.
_VALIDATOR = re.compile(r"[\x20-\x7e]{1,512}")
DATE_LIMIT = 128

_EARLIEST = datetime(1990, 1, 1, tzinfo=UTC)
_FUTURE_SLACK = timedelta(days=1)
_MAX_EPOCH = 10**11  # year 5138; anything larger is not a time


def validator(value: object, limit: int = 512) -> str | None:
    if isinstance(value, str) and len(value) <= limit and _VALIDATOR.fullmatch(value):
        return value
    return None


def load_json(body: bytes) -> Any:
    """The document, or ConnectorError. Strict UTF-8 (json.loads on bytes would also guess UTF-16
    and UTF-32); too deep or too odd a document is refused, never half read."""
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ConnectorError("bad_content", "json_invalid") from None


def moment(value: object, now: datetime) -> datetime | None:
    """An ISO 8601 text or a Unix time as a UTC time, or None if it is missing or implausible."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and 0 < value < _MAX_EPOCH:
            stamp = datetime.fromtimestamp(value, tz=UTC)
        elif isinstance(value, str) and len(value) <= 40:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                return None
            stamp = stamp.astimezone(UTC)
        else:
            return None
    except (OverflowError, OSError, ValueError):
        return None
    if stamp < _EARLIEST or stamp > now + _FUTURE_SLACK:
        return None  # a clock error or a trick to stay at the top of a list
    return stamp


def entry(
    identifier: object,
    title: object,
    excerpt: object,
    url: object,
    published: datetime | None,
) -> RawEntry | None:
    """One cleaned entry, or None if it has no usable title or link."""
    link = safe_link(url)
    clean_title = clean_text(title, TITLE_LIMIT)
    if link is None or not clean_title:
        return None
    clean_excerpt = clean_text(excerpt, EXCERPT_LIMIT)
    external = clean_identifier(identifier, EXTERNAL_ID_LIMIT) or link[:EXTERNAL_ID_LIMIT]
    digest = hashlib.sha256(f"{clean_title}\n{clean_excerpt}\n{link}".encode()).hexdigest()
    return RawEntry(external, clean_title, clean_excerpt, link, published, digest)
