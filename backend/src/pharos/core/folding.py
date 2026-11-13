# ruff: noqa: RUF001, RUF002  (the look-alike tables are in several scripts on purpose)
"""Text folded for word lists in the four languages of the app (English, Azerbaijani, Turkish and
Russian).

A list is written once, in the folded form, and matched against the folded text, so accents,
compatibility forms (full-width letters, ligatures, no-break spaces), invisible format characters
(a zero-width space, a soft hyphen) and letters of another script that look the same do not hide a
word from it. Used by the checks on a model's insight and by the hold rules of ingestion.
"""

import unicodedata

_FOLD = str.maketrans({"ə": "e", "ı": "i", "ß": "ss"})
# Letters of Cyrillic and Greek that look like Latin letters, and the other way round.
TO_LATIN = str.maketrans(
    "авекмнорстухіјѕαεικνορτυχ",
    "abekmhopctuxijsaeiknoptux",
)
TO_CYRILLIC = str.maketrans(
    "abekmhopctuxijs",
    "авекмнорстухіјѕ",
)


def folded(text: str) -> str:
    """Lower case, without accents or invisible characters, with the letters of Azerbaijani and
    Turkish made plain: `ə` and `ı` are `e` and `i`, Russian `й` and `ё` are `и` and `е`."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(
        c for c in decomposed if not unicodedata.combining(c) and unicodedata.category(c) != "Cf"
    ).translate(_FOLD)


def views(text: str) -> tuple[str, str]:
    """The folded text as a Latin-script word list sees it, and as a Cyrillic one sees it."""
    base = folded(text)
    return base.translate(TO_LATIN), base.translate(TO_CYRILLIC)
