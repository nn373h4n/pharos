"""Links from a feed: only absolute http(s) addresses are kept (ADR-0006, ADR-0009).

`javascript:`, `data:`, `file:` and friends are the point of a hostile link; a relative address is
resolved against the feed first. The rules here match the database constraint on
`items.canonical_url` (no white space, control characters, quotes or angle brackets, at most 2048
characters), so a link that passes can be stored.
"""

import re
from urllib.parse import urljoin, urlsplit

MAX_URL_LENGTH = 2048
# Backslash too: some parsers read it as a slash, so the host differs between them.
# Lone surrogates too: JSON can carry them ("\ud800"), they cannot be encoded as UTF-8, and a link
# that cannot be written down would fail the whole fetch.
_FORBIDDEN = re.compile(r"[\s\x00-\x1f\x7f-\x9f<>\"\\\ud800-\udfff]")


def safe_link(value: object, base: str | None = None) -> str | None:
    """The link as an absolute http(s) URL, or None if it is missing or unsafe."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH or _FORBIDDEN.search(candidate):
        return None
    if base is not None and safe_link(base) is not None:
        candidate = urljoin(base, candidate)
    # urlsplit drops tabs and newlines silently, so the check above must come first (it does).
    try:
        parts = urlsplit(candidate)
        port = parts.port  # raises ValueError for a malformed or out-of-range port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if "@" in parts.netloc:  # credentials inside a link are never useful and often an attack
        return None
    if len(candidate) > MAX_URL_LENGTH or _FORBIDDEN.search(candidate):
        return None
    del port
    return candidate
