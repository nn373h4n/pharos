"""Making one insight: how an article applies to a profession, in a language (M4-06, ADR-0015).

The worker runs this when a person first looks at a card. It reads the article only through
`load_public_article`, asks the strong model, and stores the answer only if it passes the checks of
`pharos.insights.validator` (a prompt that is safe to copy). An answer that never fits, or fails the
checks, is stored as `rejected` so it is not paid for again at every view; a model that is off, a
spent budget or a failing provider stores nothing, so a later view tries again.
"""

import json
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from pharos.insights.validator import validate_insight
from pharos.llm.enrichment import LANGUAGES, Generator
from pharos.llm.errors import NotPublicArticle, SchemaRejected
from pharos.llm.prompt import PROFESSION_CODE, PROMPT_VERSIONS
from pharos.llm.repository import load_public_article
from pharos.llm.schemas import InsightAnswer
from pharos.llm.types import Task

logger = logging.getLogger(__name__)


async def _enter(connection: AsyncConnection, org: uuid.UUID) -> None:
    await connection.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org)})


async def _forget_old_rejections(
    connection: AsyncConnection,
    org: uuid.UUID,
    item: uuid.UUID,
    profession: str,
    language: str,
    version: str,
) -> None:
    """A rejected answer is remembered for a day; the database allows no other delete."""
    await _enter(connection, org)
    await connection.execute(
        text(
            "DELETE FROM item_insights WHERE org_id = :org AND item_id = :item "
            "AND profession_code = :p AND language = :l AND prompt_version = :v "
            "AND status = 'rejected' AND created_at < now() - interval '24 hours'"
        ),
        {"org": org, "item": item, "p": profession, "l": language, "v": version},
    )


async def _already_made(
    connection: AsyncConnection,
    org: uuid.UUID,
    item: uuid.UUID,
    profession: str,
    language: str,
    version: str,
) -> bool:
    await _enter(connection, org)
    found = await connection.execute(
        text(
            "SELECT 1 FROM item_insights WHERE org_id = :org AND item_id = :item "
            "AND profession_code = :p AND language = :l AND prompt_version = :v"
        ),
        {"org": org, "item": item, "p": profession, "l": language, "v": version},
    )
    return found.first() is not None


async def _profession_exists(connection: AsyncConnection, org: uuid.UUID, code: str) -> bool:
    await _enter(connection, org)
    found = await connection.execute(
        text(
            "SELECT 1 FROM professions WHERE code = :code "
            "AND (scope = 'public' OR owner_org_id = :org)"
        ),
        {"code": code, "org": org},
    )
    return found.first() is not None


async def _store(
    connection: AsyncConnection,
    org: uuid.UUID,
    item: uuid.UUID,
    profession: str,
    language: str,
    version: str,
    answer: InsightAnswer | None,
) -> None:
    await _enter(connection, org)
    await connection.execute(
        text(
            "INSERT INTO item_insights (org_id, item_id, profession_code, language, "
            "prompt_version, status, summary, how_to_apply, try_prompt, safety_note) "
            "VALUES (:org, :item, :p, :l, :v, :status, :summary, CAST(:steps AS jsonb), "
            ":prompt, :note) ON CONFLICT DO NOTHING"
        ),
        {
            "org": org,
            "item": item,
            "p": profession,
            "l": language,
            "v": version,
            "status": "ok" if answer else "rejected",
            "summary": answer.summary if answer else None,
            "steps": json.dumps(answer.how_to_apply) if answer else None,
            "prompt": answer.try_prompt if answer else None,
            "note": answer.safety_note if answer else None,
        },
    )


async def make_insight(
    engine: AsyncEngine,
    gateway: Generator,
    org: uuid.UUID,
    item: uuid.UUID,
    profession: str,
    language: str,
) -> str:
    """Returns `exists`, `ok`, `rejected` or `skipped` (bad arguments, an article that is not
    public or not new any more, an unknown profession). Raises what stops the gateway (budget,
    pause, off, provider): nothing is stored then."""
    if language not in LANGUAGES or not PROFESSION_CODE.fullmatch(profession):
        return "skipped"
    version = PROMPT_VERSIONS[Task.INSIGHT]
    async with engine.begin() as connection:
        await _forget_old_rejections(connection, org, item, profession, language, version)
        if await _already_made(connection, org, item, profession, language, version):
            return "exists"
        if not await _profession_exists(connection, org, profession):
            return "skipped"
        try:
            article = await load_public_article(connection, org, item)
        except NotPublicArticle:
            return "skipped"
    try:
        answer = await gateway.generate(
            task=Task.INSIGHT,
            org_id=org,
            article=article,
            schema=InsightAnswer,
            language=language,
            profession=profession,
        )
        if not isinstance(answer, InsightAnswer):
            raise TypeError("the gateway returned another answer than the one asked for")
    except SchemaRejected:
        answer = None
    if answer is not None:
        reason = validate_insight(
            answer.summary, answer.how_to_apply, answer.try_prompt, answer.safety_note
        )
        if reason is not None:
            logger.info("insight rejected", extra={"reason": reason})
            answer = None
    async with engine.begin() as connection:
        await _store(connection, org, item, profession, language, version, answer)
    return "ok" if answer else "rejected"
