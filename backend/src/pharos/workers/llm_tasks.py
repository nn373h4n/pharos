"""Celery tasks of the model layer. Identifiers only travel through the broker."""

import asyncio
import logging
import random
import uuid
from datetime import UTC, datetime
from typing import Any

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from redis import asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.clock import SystemClock
from pharos.core.config import get_settings
from pharos.ingest import repository
from pharos.llm import clustering, enrichment, insights, keytest
from pharos.llm.errors import CallRateExceeded, LLMError
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


EMBED_TASK = "pharos.llm.embed_sweep"
EMBED_LOCK = "pharos:worker:llm:embed-sweep-lock"


@shared_task(name=EMBED_TASK, ignore_result=True)
def embed_sweep_task() -> None:
    """Embed the public articles the model has read and join near-duplicates to one cluster
    (M4-05). Runs every minute from the scheduler; safe to run twice, and a second sweep that
    starts while one is running does nothing."""
    settings = get_settings()
    if settings.embedding_provider == "off":
        return

    async def work(engine: AsyncEngine) -> None:
        async with engine.begin() as connection:
            org = await repository.resolve_org(connection, settings.org_slug)
        if org is None:
            return
        redis = aioredis.from_url(settings.redis_url.get_secret_value())
        try:
            mine = uuid.uuid4().hex
            if not await redis.set(EMBED_LOCK, mine, ex=SWEEP_LOCK_SECONDS, nx=True):
                return
            try:
                try:
                    gateway = build_gateway(
                        settings, redis, DatabaseUsageSink(engine), SystemClock()
                    )
                except Exception:  # a provider named without a key, or without LiteLLM
                    logger.warning("embedding: the model layer is not configured")
                    return
                try:
                    async with asyncio.timeout(SWEEP_TIME_LIMIT_SECONDS):
                        done = await clustering.sweep(
                            engine,
                            gateway,
                            org,
                            model=settings.embedding_model,
                            batch=settings.embedding_batch,
                            threshold=settings.embedding_similarity,
                            window_days=settings.embedding_window_days,
                        )
                except TimeoutError:
                    logger.warning("embedding sweep stopped at its time limit")
                    return
                logger.info(
                    "embedding sweep",
                    extra={
                        "embedded": done.embedded,
                        "clustered": done.clustered,
                        "failed": done.failed,
                        "stopped": done.stopped,
                    },
                )
            finally:
                await redis.eval(_UNLOCK, 1, EMBED_LOCK, mine)
        finally:
            await redis.aclose()

    runtime.run(work)


INSIGHT_TASK = "pharos.llm.insight"


# The calls to the model are counted for each wall-clock minute (30 by default, for the whole
# organization). When a burst of people asks for insights, the ones past the limit used to be
# dropped and left waiting for the five minutes the marker of their request lives (F-51). They now
# try again at the start of the next minute, at most this many times: 4 x about a minute is less
# than the five minutes the marker and the task live, so the last try is over before the person's
# next look can ask again.
INSIGHT_RETRIES = 4


@shared_task(name=INSIGHT_TASK, bind=True, ignore_result=True, max_retries=INSIGHT_RETRIES)
def insight_task(self: Any, item_id: str, profession: str, language: str) -> None:
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

    async def work(engine: AsyncEngine) -> bool:
        """True when the calls of this minute are spent and the task should try again."""
        async with engine.begin() as connection:
            org = await repository.resolve_org(connection, settings.org_slug)
        if org is None:
            return False
        redis = aioredis.from_url(settings.redis_url.get_secret_value())
        try:
            try:
                gateway = build_gateway(settings, redis, DatabaseUsageSink(engine), SystemClock())
            except Exception:  # a provider named without a key, or without LiteLLM
                logger.warning("insight: the model layer is not configured")
                return False
            try:
                outcome = await insights.make_insight(
                    engine, gateway, org, item, profession, language
                )
            except CallRateExceeded:
                logger.info("insight waits for the next minute: this minute's calls are spent")
                return True
            except LLMError as stop:
                logger.info("insight not made", extra={"reason": stop.code})
                return False
            logger.info("insight", extra={"outcome": outcome})
            return False
        finally:
            await redis.aclose()

    if runtime.run(work):
        # The count is kept for each minute of the clock: the next try is at the start of the next
        # one, spread over a few seconds so that those who waited do not all come in one second.
        wait = 60 - datetime.now(UTC).second + random.uniform(1, 10)  # noqa: S311 (spreading)
        try:
            raise self.retry(countdown=wait)
        except MaxRetriesExceededError:
            logger.info("insight not made: no free call in %d minutes", INSIGHT_RETRIES)
