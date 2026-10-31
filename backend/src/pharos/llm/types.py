"""The few types the gateway talks in."""

from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

from pharos.llm.errors import NotPublicArticle

# What is sent is cut to these lengths AFTER masking (a cut address would escape the mask).
MAX_TITLE = 300
MAX_EXCERPT = 500
# What the type keeps before that; generous, so the cut happens in one place.
_KEPT = 2000


class Stage(StrEnum):
    FILTER = "filter"  # the cheap first look
    ENRICHMENT = "enrichment"
    INSIGHT = "insight"
    KEY_TEST = "key_test"  # the administrator's "test the key"; carries no article


# How many tokens an answer may have at most, per stage. The budget reserves this much.
MAX_ANSWER_TOKENS: dict[Stage, int] = {
    Stage.FILTER: 200,
    Stage.ENRICHMENT: 600,
    Stage.INSIGHT: 900,
    Stage.KEY_TEST: 20,
}


class Task(StrEnum):
    """What the product asks a model to do. The instruction of each task is written by us, in
    `pharos.llm.prompt`; a caller names a task and passes typed parameters, never free text."""

    FILTER_RELEVANCE = "filter_relevance"
    ENRICH = "enrich"
    INSIGHT = "insight"


TASK_STAGE: dict[Task, Stage] = {
    Task.FILTER_RELEVANCE: Stage.FILTER,
    Task.ENRICH: Stage.ENRICHMENT,
    Task.INSIGHT: Stage.INSIGHT,
}


_BUILD = object()


@dataclass(frozen=True, slots=True)
class PublicArticleText:
    """Class A of ADR-0015: the public text of an organization article, and nothing else.

    It is made in one place only, `pharos.llm.repository.load_public_article`, which reads the
    article from the database and refuses one that belongs to a person, that is held or hidden, or
    that has no web address; a caller cannot hand over a row of its own. The marker that proves it
    is not a constructor argument, so neither the constructor nor `dataclasses.replace` can make
    one by hand. The gateway accepts nothing but this type, so "only public text goes out" is a
    property of the types and of one query, and not of anybody's care. A test keeps
    `mint_public_text` from being called anywhere else in the product.
    """

    title: str
    excerpt: str
    host: str
    language: str | None
    _key: object = field(default=None, init=False, repr=False, compare=False)

    def is_genuine(self) -> bool:
        return self._key is _BUILD


def mint_public_text(
    title: str, excerpt: str, canonical_url: str, language: str | None
) -> PublicArticleText:
    """Make public text from fields that the caller has already shown to be class A. Called by
    `pharos.llm.repository` only (and by tests)."""
    host = (urlsplit(canonical_url).hostname or "").lower()
    if not host:
        raise NotPublicArticle()
    built = PublicArticleText(title[:_KEPT], excerpt[:_KEPT], host, language)
    object.__setattr__(built, "_key", _BUILD)
    return built


@dataclass(frozen=True, slots=True)
class Usage:
    """What one call used. Numbers only."""

    tokens_in: int
    tokens_out: int
    cost_micro_usd: int
