"""What counts as plain text in a field that people read (and may copy).

A link, an address, markup, a control character or a character that hides or reorders text turns a
sentence into something else. The model's text is shown and copied as it is, so none of these may
be in it. Shared by the enrichment of articles and by the insights, and by the API when it serves
a stored text again.

The check looks at the text twice, as written and after compatibility normalisation (NFKC), so a
full-width slash or a compatibility letter does not hide what it is. Characters of the Unicode
categories "format" (invisible joiners, soft hyphens, direction marks), "private use", "surrogate"
and the line and paragraph separators are refused outright, as are the "tag" characters and the
variation selectors that can carry invisible text; the languages of the product need none of them.
"""

import re
import unicodedata

_REFUSED_CATEGORIES = frozenset({"Cf", "Co", "Cs", "Zl", "Zp"})
# Control characters are refused too, except the tab and the two line ends.
_ALLOWED_CONTROLS = frozenset({"\t", "\n", "\r"})

# The ranges that carry invisible text: variation selectors and their supplement, tag characters.
_HIDDEN_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF), (0xE0000, 0xE007F))

# Top-level domains common enough that a bare name is worth refusing ("evil-site.com").
_TLDS = (
    "com|net|org|info|biz|xyz|app|dev|cloud|online|site|shop|store|tech|gov|edu|top|club|live|"
    "link|click|work|ai|io|co|me|ly|az|ru|tr|uk|de|us|cc|tk|ml|cf|ga|gq"
)

_NOT_PLAIN_TEXT = re.compile(
    r"://|www\.|[<>]|\]\(|javascript:|mailto:|data:text|\b[a-z0-9-]+\.[a-z]{2,}/"
    r"|\b[a-z0-9-]+\.(?:" + _TLDS + r")\b",
    re.IGNORECASE,
)


def _hides_something(value: str) -> bool:
    for character in value:
        category = unicodedata.category(character)
        if category in _REFUSED_CATEGORIES:
            return True
        if category == "Cc" and character not in _ALLOWED_CONTROLS:
            return True
        code = ord(character)
        if any(low <= code <= high for low, high in _HIDDEN_RANGES):
            return True
    return False


def is_plain_text(value: str) -> bool:
    if _hides_something(value):
        return False
    normalised = unicodedata.normalize("NFKC", value)
    return _NOT_PLAIN_TEXT.search(value) is None and _NOT_PLAIN_TEXT.search(normalised) is None
