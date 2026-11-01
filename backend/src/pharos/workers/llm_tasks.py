"""Celery tasks of the model layer. Identifiers only travel through the broker."""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from celery import shared_task
from redis import asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.clock import SystemClock
from pharos.core.config import get_settings
from pharos.ingest import repository
from pharos.llm import enrichment, insights, keytest
from pharos.llm.errors import LLMError
from pharos.llm.factory import build_gateway
from pharos.llm.usage import DatabaseUsageSink
from pharos.workers.runtime import runtime

logger = logging.getLogger(__name__)

TEST_KEY_TASK = keytest.TASK_NAME


@shared_task(name=TEST_KEY_TASK, ignore_result=True)
def test_key_task(test_id: str) -> None:
    """One tiny call to see whether the key works (ADR-0013 pattern). The answer is left in Redis
    for a short while; nothing else is written but the record of the call. Safe to run twice."""
    try:
        test = uuid.UUID(test_id)
    except (ValueError, TypeError, AttributeError):
        logger.warning("key test: the task argument is not an identifier; dropped")
        return
    settings = get_settings()

    async def work(engine: AsyncEngine) -> None:
        async with engine.begin() as connection:
            org = await repository.resolve_org(connection, settings.org_slug)
        if org is None:
            return
        redis = aioredis.from_url(settings.redis_url.get_secret_value())
        try:
            try:
                gateway = build_gateway(settings, redis, DatabaseUsageSink(engine), SystemClock())
            except Exception:  # a provider named without a key, or without LiteLLM
                outcome = "not_configured"
            else:
                outcome = await gateway.check_key(org)
            result = keytest.KeyTestResult(
                test_id=test,
                outcome=outcome,  # type: ignore[arg-type]
                checked_at=datetime.now(UTC),
            )
            await redis.set(
                keytest.result_key(test), keytest.encode(result), ex=keytest.RESULT_TTL_SECONDS
            )
            await redis.set(
                keytest.last_key(org), keytest.encode(result), ex=keytest.LAST_TTL_SECONDS
            )
        finally:
            await redis.aclose()
        logger.info("key test", extra={"outcome": outcome})

    runtime.run(work)


SWEEP_TASK = "pharos.llm.enrich_sweep"
SWEEP_LOCK = "pharos:worker:llm:enrich-sweep-lock"
# A sweep that dies leaves the lock behind for this long at most; a normal one is much shorter.
SWEEP_LOCK_SECONDS = 600
# A sweep is stopped before its lock can run out, so two sweeps never overlap.
SWEEP_TIME_LIMIT_SECONDS = 540
# Give the lock back only if it is still ours.
_UNLOCK = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
)


@shared_task(name=SWEEP_TASK, ignore_result=True)
def enrich_sweep_task() -> None:
    """Read the public articles nobody has read yet (M4-04). Runs every minute from the scheduler;
    safe to run twice, and a second sweep that starts while one is running does nothing."""
    settings = get_settings()
    if settings.llm_provider == "off":
        return

    async def work(engine: AsyncEngine) -> None:
        async with engine.begin() as connection:
            org = await repository.resolve_org(connection, settings.org_slug)
        if org is None:
            return
        redis = aioredis.from_url(settings.redis_url.get_secret_value())
        try:
            mine = uuid.uuid4().hex
            if not await redis.set(SWEEP_LOCK, mine, ex=SWEEP_LOCK_SECONDS, nx=True):
                return
            try:
                clock = SystemClock()
                try:
                    gateway = build_gateway(settings, redis, DatabaseUsageSink(engine), clock)
                except Exception:  # a provider named without a key, or without LiteLLM
                    logger.warning("enrichment: the model layer is not configured")
                    return
                try:
                    async with asyncio.timeout(SWEEP_TIME_LIMIT_SECONDS):
                        done = await enrichment.sweep(
                            engine, gateway, org, batch=settings.llm_enrich_batch, now=clock.now
                        )
                except TimeoutError:
                    logger.warning("enrichment sweep stopped at its time limit")
                    return
                logger.info(
                    "enrichment sweep",
                    extra={
                        "read": done.read,
                        "held": done.held,
                        "failed": done.failed,
                        "stopped": done.stopped,
                    },
                )
            finally:
                await redis.eval(_UNLOCK, 1, SWEEP_LOCK, mine)
        finally:
            await redis.aclose()

    runtime.run(work)


INSIGHT_TASK = "pharos.llm.insight"


@shared_task(name=INSIGHT_TASK, ignore_result=True)
def insight_task(item_id: str, profession: str, language: str) -> None:
    """Make the insight of one article for a profession and a language (M4-06). Identifiers and
    two words only come through the broker; everything is checked again here. Safe to run twice."""
    try:
        item = uuid.UUID(item_id)
    except (ValueError, TypeError, AttributeError):
        logger.warning("insight: the task argument is not an identifier; dropped")
        return
    if not isinstance(profession, str) or not isinstance(language, str):
        return
    settings = get_settings()
    if settings.llm_provider == "off":
        return

    async def work(engine: AsyncEngine) -> None:
        async with engine.begin() as connection:
            org = await repository.resolve_org(connection, settings.org_slug)
        if org is None:
            return
        redis = aioredis.from_url(settings.redis_url.get_secret_value())
        try:
            try:
                gateway = build_gateway(settings, redis, DatabaseUsageSink(engine), SystemClock())
            except Exception:  # a provider named without a key, or without LiteLLM
                logger.warning("insight: the model layer is not configured")
                return
            try:
                outcome = await insights.make_insight(
                    engine, gateway, org, item, profession, language
                )
            except LLMError as stop:
                logger.info("insight not made", extra={"reason": stop.code})
                return
            logger.info("insight", extra={"outcome": outcome})
        finally:
            await redis.aclose()

    runtime.run(work)
