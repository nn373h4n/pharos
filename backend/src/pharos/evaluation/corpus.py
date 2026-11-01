"""The articles an evaluation is run on: a JSON file the operator can read and edit.

Each entry is public article text (a title, an excerpt, an address) with the profession it is meant
for and a human label, `relevant`, which says whether it is about practical use of AI at work (used
only to judge the cheap filter). The loader is strict: the file is trusted no more than a feed.
"""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from pharos.ingest.links import safe_link
from pharos.ingest.sourceconfig import ConfigError, refuse_internal_host
from pharos.ingest.text import clean_text
from pharos.llm.prompt import PROFESSION_CODE

LANGUAGES = frozenset({"az", "tr", "ru", "en"})
MAX_ITEMS = 600
MAX_TITLE = 300
MAX_EXCERPT = 500
MAX_FILE_BYTES = 2_000_000
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class CorpusError(ValueError):
    """The corpus file is not acceptable. The message names the entry, never its text."""


@dataclass(frozen=True, slots=True)
class CorpusItem:
    id: str
    title: str
    excerpt: str
    url: str
    lang: str | None
    profession: str
    #: A human's label: is it about practical use of AI at work? None until someone has decided.
    relevant: bool | None


def _check(entry: object, position: int) -> CorpusItem:
    where = f"entry {position}"
    if not isinstance(entry, dict):
        raise CorpusError(f"{where}: not an object")
    expected = {"id", "title", "excerpt", "url", "lang", "profession", "relevant"}
    if set(entry) != expected:
        raise CorpusError(f"{where}: the fields must be exactly {sorted(expected)}")
    ident, title, excerpt = entry["id"], entry["title"], entry["excerpt"]
    if not isinstance(ident, str) or not _ID.fullmatch(ident):
        raise CorpusError(f"{where}: id must be 1 to 40 letters, digits, - or _")
    where = f"entry {ident!r}"
    if not isinstance(title, str) or not 1 <= len(title) <= MAX_TITLE:
        raise CorpusError(f"{where}: title must be 1 to {MAX_TITLE} characters")
    if not isinstance(excerpt, str) or len(excerpt) > MAX_EXCERPT:
        raise CorpusError(f"{where}: excerpt must be at most {MAX_EXCERPT} characters")
    if clean_text(title, MAX_TITLE) != title or clean_text(excerpt, MAX_EXCERPT) != excerpt:
        raise CorpusError(
            f"{where}: title and excerpt must be plain text (no tags, entities, hidden characters)"
        )
    url = safe_link(entry["url"])
    if url is None:
        raise CorpusError(f"{where}: url must be a plain http(s) address")
    try:
        # A corpus is public text from the open web: an address inside a network is not that.
        refuse_internal_host(url, ())
    except ConfigError:
        raise CorpusError(f"{where}: url points inside a network, not at the public web") from None
    lang = entry["lang"]
    if lang is not None and lang not in LANGUAGES:
        raise CorpusError(f"{where}: lang must be one of {sorted(LANGUAGES)} or null")
    profession = entry["profession"]
    if not isinstance(profession, str) or not PROFESSION_CODE.fullmatch(profession):
        raise CorpusError(f"{where}: profession must be a catalogue code")
    if entry["relevant"] is not None and not isinstance(entry["relevant"], bool):
        raise CorpusError(f"{where}: relevant must be true, false or null")
    return CorpusItem(ident, title, excerpt, url, lang, profession, entry["relevant"])


def parse(raw: object) -> list[CorpusItem]:
    if not isinstance(raw, list) or not raw:
        raise CorpusError("the corpus must be a non-empty list")
    if len(raw) > MAX_ITEMS:
        raise CorpusError(f"the corpus may hold at most {MAX_ITEMS} entries")
    items = [_check(entry, position) for position, entry in enumerate(raw, start=1)]
    ids = [item.id for item in items]
    if len(set(ids)) != len(ids):
        raise CorpusError("ids must be unique")
    return items


def load(path: Path) -> list[CorpusItem]:
    try:
        size = path.stat().st_size
    except OSError:
        raise CorpusError("cannot read the corpus: OSError") from None
    if size > MAX_FILE_BYTES:
        raise CorpusError("the corpus file is too large")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        raise CorpusError(f"cannot read the corpus: {type(error).__name__}") from None
    return parse(raw)


def require_labels(items: list[CorpusItem]) -> None:
    """A run needs a human's label on every article (the cheap filter is judged against them)."""
    open_ones = [item.id for item in items if item.relevant is None]
    if open_ones:
        raise CorpusError(
            f"{len(open_ones)} article(s) have no label yet (relevant is null), for example "
            f"{open_ones[0]!r}: set each to true or false"
        )


def dump(items: list[CorpusItem], path: Path) -> None:
    path.write_text(
        json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
