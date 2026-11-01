"""Reading public articles with a model: a summary, a category, a quality score and how well the
article fits each profession (M4-04, ADR-0015 and ADR-0017).

One sweep takes a few articles that nobody has read yet, one at a time. For each it asks the small
model whether the article is about practical use of AI at work, and then the strong model for the
summary and the scores. The answer is checked beyond its schema before anything is stored: text
with a link or markup in it is never kept, and a profession code the catalogue does not have is
dropped. An article judged poor or irrelevant, or whose answer is suspicious or never fits the
schema, is held for the editor queue with the reason `low_quality`; the worker has the right to do
that and nothing else to an article's state.

Nothing here reads an article except `load_public_article`, so a personal article is never sent.
A model that is off, a budget that is spent, a stage that is resting or a provider that fails ends
the sweep quietly; the articles stay where they were and the next sweep tries again.
"""

import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from pharos.llm.errors import (
    BudgetExhausted,
    BudgetUnavailable,
    CallRateExceeded,
    LLMDisabled,
    LLMNotConfigured,
    NotPublicArticle,
    ProviderFailed,
    ProviderRejected,
    SchemaRejected,
    StagePaused,
)
from pharos.llm.prompt import MAX_PROFESSIONS, PROMPT_VERSIONS
from pharos.llm.repository import load_public_article
from pharos.llm.schemas import EnrichAnswer, FilterAnswer
from pharos.llm.types import PublicArticleText, Task

logger = logging.getLogger(__name__)

HOLD_REASON = "low_quality"
# What the answer schema and the prompt can carry as a profession code.
_CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
LANGUAGES = frozenset({"en", "az", "tr", "ru"})
# The small model must be this sure that an article is NOT about practical AI at work before the
# article is held without a look from the strong model.
IRRELEVANT_CONFIDENCE = 70
# A quality score this low or lower is held for an editor.
HOLD_QUALITY_AT_MOST = 2
# This many articles in a row that cannot be read end the sweep: the fault is probably not theirs.
MAX_FAILURES_IN_A_ROW = 3

# A link, an address, markup or a markdown link in a field that is shown to people.
# Also control characters, and the characters that hide or reorder text.
_NOT_PLAIN_TEXT = re.compile(
    r"://|www\.|[<>]|\]\(|\b[a-z0-9-]+\.[a-z]{2,}/|javascript:|mailto:"
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]",
    re.IGNORECASE,
)

# The reasons a sweep stops early. None of them is the article's fault.
_STOPS = (
    BudgetExhausted,
    BudgetUnavailable,
    CallRateExceeded,
    LLMDisabled,
    LLMNotConfigured,
    ProviderFailed,
    StagePaused,
)


class Generator(Protocol):
    async def generate(
        self,
        *,
        task: Task,
        org_id: uuid.UUID,
        article: PublicArticleText,
        schema: type[EnrichAnswer] | type[FilterAnswer],
        language: str | None = None,
        profession: str | None = None,
        professions: Sequence[str] | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class Enriched:
    summary: str
    category: str
    quality: int
    professions: dict[str, int]


@dataclass(slots=True)
class SweepResult:
    read: int = 0
    held: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: str | None = None  # the code of the reason the sweep ended early


def is_plain_text(value: str) -> bool:
    return _NOT_PLAIN_TEXT.search(value) is None


def check_answer(answer: EnrichAnswer, known: frozenset[str]) -> Enriched | None:
    """The answer in the form that is stored, or None if it is suspicious (it carries a link or
    markup). Codes the catalogue does not have are dropped; a repeated code keeps its best score."""
    if not is_plain_text(answer.summary):
        return None
    scores: dict[str, int] = {}
    for item in answer.professions:
        if item.code in known:
            scores[item.code] = max(item.relevance, scores.get(item.code, 0))
    return Enriched(answer.summary, answer.category, answer.quality, scores)


def language_for(article: PublicArticleText) -> str:
    return article.language if article.language in LANGUAGES else "en"


# --- the SQL (as the worker role, inside row-level security) ---


async def _enter(connection: AsyncConnection, org: uuid.UUID) -> None:
    await connection.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org)})


async def pending_items(connection: AsyncConnection, org: uuid.UUID, limit: int) -> list[uuid.UUID]:
    """Public, new articles nobody has read yet, oldest first."""
    await _enter(connection, org)
    rows = await connection.execute(
        text(
            "SELECT id FROM items WHERE org_id = :org AND owner_user_id IS NULL "
            "AND status = 'new' AND enriched_at IS NULL AND reviewed_at IS NULL "
            "ORDER BY created_at, id LIMIT :n"
        ),
        {"org": org, "n": limit},
    )
    return [row.id for row in rows]


async def profession_codes(connection: AsyncConnection, org: uuid.UUID) -> frozenset[str]:
    """The codes the model may use: the public ones and the organization's own."""
    await _enter(connection, org)
    rows = await connection.execute(
        text("SELECT code FROM professions WHERE scope = 'public' OR owner_org_id = :org"),
        {"org": org},
    )
    # Only codes the prompt and the answer schema can carry, and at most the prompt's limit.
    usable = sorted(row.code for row in rows if _CODE.fullmatch(row.code))
    return frozenset(usable[:MAX_PROFESSIONS])


async def store(
    connection: AsyncConnection,
    org: uuid.UUID,
    item: uuid.UUID,
    *,
    now: datetime,
    version: str,
    result: Enriched | None,
    hold: bool,
) -> bool:
    """Write what was learned. Only a row that is still `new` and still unread is touched, so a
    decision an editor made meanwhile stands. Returns False when there was nothing to write."""
    await _enter(connection, org)
    done = await connection.execute(
        text(
            "UPDATE items SET summary = :summary, category = :category, quality = :quality, "
            "enriched_at = :now, enrichment_version = :version, "
            "status = CASE WHEN :hold THEN 'held' ELSE status END, "
            "hold_reason = CASE WHEN :hold THEN :reason ELSE hold_reason END "
            "WHERE org_id = :org AND id = :id AND owner_user_id IS NULL "
            "AND status = 'new' AND enriched_at IS NULL AND reviewed_at IS NULL"
        ),
        {
            "summary": result.summary if result else None,
            "category": result.category if result else None,
            "quality": result.quality if result else None,
            "now": now,
            "version": version,
            "hold": hold,
            "reason": HOLD_REASON,
            "org": org,
            "id": item,
        },
    )
    if done.rowcount != 1:
        return False
    if result is not None:
        for code, relevance in result.professions.items():
            await connection.execute(
                text(
                    "INSERT INTO item_professions (org_id, item_id, profession_code, relevance) "
                    "VALUES (:org, :item, :code, :relevance)"
                ),
                {"org": org, "item": item, "code": code, "relevance": relevance},
            )
    return True


# --- the sweep ---


async def sweep(
    engine: AsyncEngine,
    gateway: Generator,
    org: uuid.UUID,
    *,
    batch: int,
    now: Callable[[], datetime],
) -> SweepResult:
    """Read up to `batch` unread public articles. See the module docstring."""
    result = SweepResult()
    async with engine.begin() as connection:
        ids = await pending_items(connection, org, batch)
        known = await profession_codes(connection, org)
    rejected: list[uuid.UUID] = []
    in_a_row = 0
    for item in ids:
        try:
            outcome = await _one(engine, gateway, org, item, known, now)
        except _STOPS as stop:
            result.stopped = stop.code if hasattr(stop, "code") else type(stop).__name__
            logger.info("enrichment sweep ended early", extra={"reason": result.stopped})
            break
        except (ProviderRejected, Exception) as error:
            # One article that cannot be read must not stop the others (a provider can refuse a
            # text it dislikes; a bug can fail on one row). A wrong key looks the same, so after
            # three in a row the sweep ends and the articles are left alone.
            in_a_row += 1
            result.failed += 1
            if isinstance(error, ProviderRejected):
                rejected.append(item)
            logger.warning(
                "enrichment of one article failed", extra={"error": type(error).__name__}
            )
            if in_a_row >= MAX_FAILURES_IN_A_ROW:
                result.stopped = "repeated_failures"
                break
            continue
        in_a_row = 0
        if outcome == "skipped":
            result.skipped += 1
        else:
            result.read += 1
            result.held += outcome == "held"
    if result.read:
        # Other articles were read in the same sweep, so the key and the provider work: the
        # refused ones are the problem, and are held for an editor instead of being tried every
        # minute for ever.
        for item in rejected:
            if await _hold_unread(engine, org, item, now):
                result.held += 1
    return result


async def _hold_unread(
    engine: AsyncEngine, org: uuid.UUID, item: uuid.UUID, now: Callable[[], datetime]
) -> bool:
    async with engine.begin() as connection:
        return await store(
            connection,
            org,
            item,
            now=now(),
            version=PROMPT_VERSIONS[Task.ENRICH],
            result=None,
            hold=True,
        )


async def _one(
    engine: AsyncEngine,
    gateway: Generator,
    org: uuid.UUID,
    item: uuid.UUID,
    known: frozenset[str],
    now: Callable[[], datetime],
) -> str:
    version = PROMPT_VERSIONS[Task.ENRICH]
    async with engine.begin() as connection:
        try:
            article = await load_public_article(connection, org, item)
        except NotPublicArticle:
            return "skipped"  # an editor acted, or the article was removed, since the list was made
    language = language_for(article)

    async def finish(result: Enriched | None, *, hold: bool) -> str:
        async with engine.begin() as connection:
            written = await store(
                connection, org, item, now=now(), version=version, result=result, hold=hold
            )
        if not written:
            return "skipped"
        return "held" if hold else "read"

    try:
        decision = await gateway.generate(
            task=Task.FILTER_RELEVANCE, org_id=org, article=article, schema=FilterAnswer
        )
        if not isinstance(decision, FilterAnswer):
            raise TypeError("the gateway returned another answer than the one asked for")
        if not decision.relevant and decision.confidence >= IRRELEVANT_CONFIDENCE:
            return await finish(None, hold=True)
        answer = await gateway.generate(
            task=Task.ENRICH,
            org_id=org,
            article=article,
            schema=EnrichAnswer,
            language=language,
            professions=sorted(known),
        )
        if not isinstance(answer, EnrichAnswer):
            raise TypeError("the gateway returned another answer than the one asked for")
    except SchemaRejected:
        # The model never gave an answer of the right shape, in spite of the allowed retries.
        # Retrying forever would only cost money: an editor looks at it.
        return await finish(None, hold=True)
    checked = check_answer(answer, known)
    if checked is None:
        return await finish(None, hold=True)
    return await finish(checked, hold=checked.quality <= HOLD_QUALITY_AT_MOST)
