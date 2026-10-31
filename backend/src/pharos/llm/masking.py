"""Masking of text before it leaves for a model (ADR-0015, point 3).

Defence in depth: only public article text is sent, but an author's address or a number in an
excerpt must not travel to a vendor either. The patterns are deliberately plain; they catch the
common shapes, not every one, which is why the data classes of ADR-0015 are the real boundary.
"""

import re
from dataclasses import dataclass

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
# Two letters, two digits, then groups of letters and digits; spaces between groups are allowed.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_LABELLED_ID = re.compile(
    r"\b(?:FIN|SSN|TCKN|TC|ID|VOEN|INN|NIN)\b[ :#.\-]*(?=[A-Z0-9\-]*\d)[A-Z0-9\-]{6,}",
    re.IGNORECASE,
)
_ELEVEN_DIGITS = re.compile(r"(?<!\d)\d{11}(?!\d)")
_PHONE = re.compile(r"(?<![\w])[+(]?\d[\d ()\-.]{7,}\d(?![\w])")


@dataclass(frozen=True, slots=True)
class Masked:
    text: str
    count: int


def _phone(match: re.Match[str]) -> str:
    digits = sum(character.isdigit() for character in match.group())
    floor = 7 if match.group().startswith("+") else 9
    return "[PHONE]" if floor <= digits <= 15 else match.group()


def mask(text: str) -> Masked:
    """The text with e-mail addresses, IBANs, labelled identity numbers, eleven-digit numbers and
    telephone numbers replaced by a word in brackets, and how many things were replaced."""
    count = 0
    for pattern, word in (
        (_EMAIL, "[EMAIL]"),
        (_IBAN, "[IBAN]"),
        (_LABELLED_ID, "[ID]"),
        (_ELEVEN_DIGITS, "[ID]"),
    ):
        text, found = pattern.subn(word, text)
        count += found
    changed = 0

    def phone(match: re.Match[str]) -> str:
        nonlocal changed
        replaced = _phone(match)
        changed += replaced != match.group()
        return replaced

    text = _PHONE.sub(phone, text)
    return Masked(text, count + changed)
