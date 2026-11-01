# ruff: noqa: RUF001, E501  (the word lists and look-alike tables are in several scripts on purpose)
"""What a model's insight must pass before it is stored and again before it is served (risk R1).

The "Try this" prompt is meant to be copied into another AI tool, so it is the place where a
person could paste something they should not. The rules are plain and strict, and they are applied
to text the product made from a stranger's article:

* every text is plain: no link, address, markup or hidden character (`core.plaintext`);
* the prompt holds at least one placeholder in square brackets, `[TEXT OF THE DOCUMENT]`, where the
  person puts their own text, so the prompt itself carries no real data; what is inside a
  placeholder names a document or a topic, never a person or personal data;
* nothing in it looks like data (an e-mail address, a telephone number, an IBAN, an identity
  number: whatever the masking of the model layer would mask);
* it does not ask for, or even name, a password, a key, a card number, an identity document, a
  login, a date of birth or personal data, in English, Azerbaijani, Turkish and Russian. The word
  lists are a floor, not a proof; they are reviewed by native speakers (F-01) and the card also
  carries a data note.

The words are looked for in the text as written and in three foldings of it, so accents, look-alike
letters of another script and leetspeak do not hide them, and, for the long stems, with all
separators removed ("p a s s - w o r d"). Short words are not looked for across separators.

The answer is a reason word or None, never the text, so it is safe to log.
"""

import re
import unicodedata
from collections.abc import Sequence

from pharos.core.plaintext import is_plain_text
from pharos.llm.masking import mask

MAX_SUMMARY = 400
MAX_STEP = 300
MAX_STEPS = 3
MAX_PROMPT = 800
MAX_NOTE = 300

_PLACEHOLDER = re.compile(r"\[([^\[\]\r\n]{2,60})\]")

# --- foldings --------------------------------------------------------------------------------------------

_FOLD = str.maketrans({"ə": "e", "ı": "i", "ß": "ss"})
# Letters of Cyrillic and Greek that look like Latin letters, and the other way round.
_TO_LATIN = str.maketrans(
    "авекмнорстухіјѕαεικνορτυχ",
    "abekmhopctuxijsaeiknoptux",
)
_TO_CYRILLIC = str.maketrans(
    "abekmhopctuxijs",
    "авекмнорстухіјѕ",
)
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)


def _folded(text: str) -> str:
    """Lower case, without accents, with the letters of Azerbaijani and Turkish made plain."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.translate(_FOLD)


def _views(text: str) -> tuple[str, str, str]:
    """The text as the Latin-script word lists see it, as the Cyrillic one sees it, and with the
    separators removed and digits read as letters (for the long stems)."""
    base = _folded(text)
    latin = base.translate(_TO_LATIN)
    cyrillic = base.translate(_TO_CYRILLIC)
    squashed = re.sub(r"[^a-z0-9]", "", latin.translate(_LEET))
    return latin, cyrillic, squashed


# --- the word lists (written in the folded form) ----------------------------------------------------------

_LATIN_WORDS = re.compile(
    "|".join(
        [
            # English
            r"\bpass(?:word|code|phrase)s?\b",
            r"\bpin\b",
            r"\bapi[ _-]?keys?\b",
            r"\bsecret(?:s| key)?\b",
            r"\b(?:access |auth(?:orization)? |bearer )?tokens?\b",
            r"\bcredentials?\b",
            r"\b(?:credit |debit |bank )?card(?: number)?s?\b",
            r"\bcvv\b",
            r"\biban\b",
            r"\bssn\b",
            r"\bsocial security\b",
            r"\bpassports?\b",
            r"\b(?:identity|id) (?:number|card)\b",
            r"\bpersonal (?:data|information)\b",
            r"\bbank account\b",
            r"\baccount number\b",
            r"\blog ?in (?:details|name|id)\b",
            r"\buser ?names?\b",
            r"\bdate of birth\b",
            r"\bbirth ?date\b",
            # Azerbaijani
            r"\bparol\w*",
            r"\bsifre\w*",
            r"\bkart(?:in)? nomre\w*",
            r"\bpasport\w*",
            r"\bsexsiyyet vesiqe\w*",
            r"\bsexsi melumat\w*",
            r"\bbank hesab\w*",
            r"\bhesab nomre\w*",
            r"\bistifadeci adi\b",
            r"\bdogum tarix\w*",
            r"\bvoen\b",
            r"\bfin kod\w*",
            # Turkish
            r"\bparola\w*",
            r"\bkart numara\w*",
            r"\bkimlik (?:numara|kart|no\b)\w*",
            r"\btc kimlik\w*",
            r"\bkisisel veri\w*",
            r"\bbanka hesab\w*",
            r"\bhesap numara\w*",
            r"\bkullanici adi\b",
            r"\bdogum tarih\w*",
            r"\btckn\b",
        ]
    ),
    re.IGNORECASE,
)

_CYRILLIC_WORDS = re.compile(
    "|".join(
        [
            r"\bпарол\w*",
            r"\bтокен\w*",
            r"\bсекретн\w+ ключ\w*",
            r"\bapi[- ]?ключ\w*",
            r"\bномер\w* (?:банковской )?карт\w*",
            r"\bпаспорт\w*",
            r"\bинн\b",
            r"\bснилс\b",
            r"\bбанковск\w+ сч[её]т\w*",
            r"\bномер\w* сч[её]т\w*",
            r"\bперсональн\w+ данн\w*",
            r"\bличн\w+ данн\w*",
            r"\bлогин\w*",
            r"\bимя пользователя\b",
            r"\bдат\w+ рожден\w*",
        ]
    ),
    re.IGNORECASE,
)

# Stems long enough to be looked for with the separators removed. No word boundaries here.
_SQUASHED_STEMS = re.compile(
    "|".join(
        [
            "password",
            "passcode",
            "passphrase",
            "apikey",
            "secretkey",
            "accesstoken",
            "authtoken",
            "bearertoken",
            "credential",
            "creditcard",
            "debitcard",
            "cardnumber",
            "socialsecurity",
            "passport",
            "personaldata",
            "personalinformation",
            "bankaccount",
            "accountnumber",
            "dateofbirth",
            "username",
            "logindetails",
        ]
    )
)

# What a placeholder may not ask for: it names a document or a topic, never a person or their data.
_PLACEHOLDER_DATA = re.compile(
    "|".join(
        [
            r"\bnames?\b",
            r"\bsurnames?\b",
            r"\bsalar\w*",
            r"\bwages?\b",
            r"\baddress\w*",
            r"\bphones?\b",
            r"\be-?mails?\b",
            r"\bbirth\w*",
            r"\baccounts?\b",
            r"\blogin\b",
            r"\busers?\b",
            r"\bad soyad\w*",
            r"\bsoyad\w*",
            r"\bisim\w*",
            r"\bmaas\w*",
            r"\bemek haqq\w*",
            r"\badres\w*",
            r"\btelefon\w*",
            r"\bdogum\w*",
            r"\bhesab\w*",
            r"\bhesap\w*",
        ]
    ),
    re.IGNORECASE,
)
_PLACEHOLDER_DATA_CYRILLIC = re.compile(
    r"\b(?:имя|имена|имён|фамили\w+|зарплат\w*|оклад\w*|адрес\w*|телефон\w*|рожден\w*|сч[её]т\w*|логин\w*|почт\w*)\b",
    re.IGNORECASE,
)


def _mentions_secrets(text: str) -> bool:
    for view in (text, *_views(text)[:2]):
        if _LATIN_WORDS.search(view) or _CYRILLIC_WORDS.search(view):
            return True
    return _SQUASHED_STEMS.search(_views(text)[2]) is not None


def _placeholder_asks_for_data(text: str) -> bool:
    for found in _PLACEHOLDER.finditer(text):
        inside = found.group(1)
        latin, cyrillic, _ = _views(inside)
        if _PLACEHOLDER_DATA.search(latin) or _PLACEHOLDER_DATA_CYRILLIC.search(cyrillic):
            return True
    return False


def validate_try_prompt(prompt: str) -> str | None:
    """None if the prompt may be shown, else the reason."""
    if not prompt.strip() or len(prompt) > MAX_PROMPT:
        return "length"
    if not is_plain_text(prompt):
        return "not_plain_text"
    if _PLACEHOLDER.search(prompt) is None:
        return "no_placeholder"
    if mask(prompt).count or mask(unicodedata.normalize("NFKC", prompt)).count:
        return "looks_like_data"
    if _mentions_secrets(prompt):
        return "sensitive_word"
    if _placeholder_asks_for_data(prompt):
        return "placeholder_asks_for_data"
    return None


def validate_insight(
    summary: str, steps: Sequence[str], try_prompt: str, safety_note: str
) -> str | None:
    """None if the whole insight may be stored and shown, else the first reason found."""
    if not isinstance(steps, (list, tuple)) or not all(
        isinstance(text, str) for text in (summary, try_prompt, safety_note, *steps)
    ):
        return "length"
    texts = [summary, *steps, safety_note]
    if (
        not summary.strip()
        or len(summary) > MAX_SUMMARY
        or not 1 <= len(steps) <= MAX_STEPS
        or any(not step.strip() or len(step) > MAX_STEP for step in steps)
        or not safety_note.strip()
        or len(safety_note) > MAX_NOTE
    ):
        return "length"
    if not all(is_plain_text(text) for text in texts):
        return "not_plain_text"
    return validate_try_prompt(try_prompt)
