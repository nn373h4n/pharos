"""Turning text from a feed into plain, bounded, printable text (ADR-0009).

A title or a summary from a feed can contain markup, scripts, look-alike and invisible characters,
control characters that break the database or a terminal, and arbitrary length. What leaves this
module is plain text: no markup (escaped markup in the source comes out as the literal characters,
so the text must always be escaped when shown), NFKC-normalized, none of the characters that
change how text reads (bidirectional controls, zero-width characters) or that PostgreSQL refuses
(NUL), single spaces between words, and at most `limit` characters. It is stored and shown as text;
it is never interpreted as HTML.
"""

import html
import re
import unicodedata

import nh3

# More than this much input is never looked at: a 2 MiB feed entry must not cost 2 MiB of cleaning.
_MAX_INPUT_CHARS = 100_000
# Control (Cc), format (Cf: zero-width and bidirectional controls), surrogate, private use,
# unassigned.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
# Characters that separate words even though they are controls or separators.
_SPACE_LIKE = "\t\n\r\x0b\x0c\x85" + chr(0x2028) + chr(0x2029)  # line and paragraph separators
_WHITESPACE = re.compile(r"\s+")
ELLIPSIS = "…"


# Invisible or blank-looking characters that are not in a "control" category: variation selectors
# (they can carry hidden text), Hangul and Khmer fillers, the combining grapheme joiner and the
# Mongolian free variation selectors.
_INVISIBLE = frozenset(
    [
        0x034F,
        0x115F,
        0x1160,
        0x17B4,
        0x17B5,
        0x3164,
        0xFFA0,
        *range(0x180B, 0x180F),
        *range(0xFE00, 0xFE10),
        *range(0xE0100, 0xE01F0),
    ]
)


def _droppable(char: str) -> bool:
    return unicodedata.category(char) in _DROPPED_CATEGORIES or ord(char) in _INVISIBLE


def _printable(text: str) -> str:
    kept: list[str] = []
    for char in text:
        if char in _SPACE_LIKE:
            kept.append(" ")
        elif not _droppable(char):
            kept.append(char)
    return "".join(kept)


def clean_text(value: object, limit: int) -> str:
    """Plain text of `value`, at most `limit` characters, "" if there is nothing usable."""
    if not isinstance(value, str) or not value or limit <= 0:
        return ""
    # No tag is allowed and no attribute: what is left is the text between the tags. `script` and
    # `style` content is dropped with its tags. Entities come back from nh3 escaped; unescape once.
    # A lone surrogate cannot be encoded and would make the cleaner raise: drop it first.
    safe = value[:_MAX_INPUT_CHARS].encode("utf-8", "ignore").decode("utf-8")
    stripped = nh3.clean(safe, tags=set(), attributes={}, strip_comments=True)
    text = unicodedata.normalize("NFKC", html.unescape(stripped))
    text = _WHITESPACE.sub(" ", _printable(text)).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rstrip()
    # Do not leave a combining mark without its base letter at the end.
    while cut and unicodedata.category(cut[-1]).startswith("M"):
        cut = cut[:-1]
    return cut + ELLIPSIS


def clean_identifier(value: object, limit: int) -> str:
    """An identifier from a feed (a guid): no control characters, no outer white space, bounded."""
    if not isinstance(value, str):
        return ""
    printable = "".join(c for c in value if not _droppable(c) and c not in _SPACE_LIKE)
    return printable.strip()[:limit]
