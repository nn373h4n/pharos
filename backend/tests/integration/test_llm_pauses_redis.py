"""The shared pause of a failing stage on the real Redis, as the worker's Redis user (F-40 c)."""

import pytest
from redis.asyncio import Redis

from pharos.llm.pauses import AFTER, RedisPauses, failures_key, pause_key
from pharos.llm.types import Stage

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_a_stage_rests_for_every_task_and_the_worker_user_may_keep_the_keys(
    redis_worker_url: str,
) -> None:
    redis = Redis.from_url(redis_worker_url, decode_responses=True)
    stage = Stage.KEY_TEST  # no other test uses its keys
    try:
        await redis.delete(pause_key(stage), failures_key(stage))
        for _ in range(AFTER):
            await RedisPauses(redis).failed(stage)  # each task builds its own gateway
        assert await RedisPauses(redis).paused(stage) is True
        assert 55_000 <= await redis.pttl(pause_key(stage)) <= 60_000
        assert await redis.exists(failures_key(stage)) == 0
        await redis.delete(pause_key(stage))
        assert await RedisPauses(redis).paused(stage) is False
    finally:
        await redis.delete(pause_key(stage), failures_key(stage))
        await redis.aclose()
