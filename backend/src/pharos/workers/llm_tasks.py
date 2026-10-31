"""Celery tasks of the model layer. Identifiers only travel through the broker."""

import logging
import uuid
from datetime import UTC, datetime

from celery import shared_task
from redis import asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.clock import SystemClock
from pharos.core.config import get_settings
from pharos.ingest import repository
from pharos.llm import keytest
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
