"""A failing stage's rest is shared through Redis, so each new gateway still sees it (F-40 c)."""

import pytest

from pharos.llm.pauses import (
    AFTER,
    FAILURE_WINDOW_SECONDS,
    KEY_PREFIX,
    LocalPauses,
    RedisPauses,
    failures_key,
    pause_key,
)
from pharos.llm.types import Stage

pytestmark = pytest.mark.anyio


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
        self.broken = False

    def _check(self) -> None:
        if self.broken:
            raise ConnectionError("redis is down")

    async def exists(self, *names: str) -> int:
        self._check()
        return sum(name in self.values for name in names)

    async def incr(self, name: str) -> int:
        self._check()
        self.values[name] = self.values.get(name, 0) + 1
        return self.values[name]

    async def expire(self, name: str, time: int) -> bool:
        self._check()
        self.ttl[name] = time
        return True

    async def set(self, name: str, value: str, *, px: int) -> bool:
        self._check()
        self.values[name] = 1
        self.ttl[name] = px
        return True

    async def delete(self, *names: str) -> int:
        self._check()
        return sum(self.values.pop(name, None) is not None for name in names)


async def test_five_failures_in_a_row_rest_the_stage_for_every_new_gateway() -> None:
    redis = FakeRedis()
    for _ in range(AFTER - 1):
        await RedisPauses(redis).failed(Stage.INSIGHT)  # a new object each time, as per task
        assert await RedisPauses(redis).paused(Stage.INSIGHT) is False
    await RedisPauses(redis).failed(Stage.INSIGHT)
    assert await RedisPauses(redis).paused(Stage.INSIGHT) is True
    assert await RedisPauses(redis).paused(Stage.ENRICHMENT) is False  # only that stage
    assert redis.ttl[pause_key(Stage.INSIGHT)] == 60_000
    assert failures_key(Stage.INSIGHT) not in redis.values  # the count starts again after a rest


async def test_a_success_starts_the_count_again_and_the_count_expires() -> None:
    redis = FakeRedis()
    pauses = RedisPauses(redis)
    for _ in range(AFTER - 1):
        await pauses.failed(Stage.FILTER)
    assert redis.ttl[failures_key(Stage.FILTER)] == FAILURE_WINDOW_SECONDS
    await pauses.worked(Stage.FILTER)
    await pauses.failed(Stage.FILTER)
    assert await pauses.paused(Stage.FILTER) is False


async def test_the_keys_are_under_the_workers_own_prefix() -> None:
    assert KEY_PREFIX.startswith("pharos:worker:")
    for stage in Stage:
        assert pause_key(stage).startswith(KEY_PREFIX)
        assert failures_key(stage).startswith(KEY_PREFIX)


async def test_without_redis_the_process_keeps_its_own_count() -> None:
    redis = FakeRedis()
    redis.broken = True
    pauses = RedisPauses(redis, fallback=LocalPauses(lambda: 100.0))
    for _ in range(AFTER):
        await pauses.failed(Stage.INSIGHT)  # never raises
    assert await pauses.paused(Stage.INSIGHT) is True


async def test_the_local_pause_ends_after_its_seconds() -> None:
    now = [0.0]
    pauses = LocalPauses(lambda: now[0])
    for _ in range(AFTER):
        await pauses.failed(Stage.EMBEDDING)
    assert await pauses.paused(Stage.EMBEDDING) is True
    now[0] = 61.0
    assert await pauses.paused(Stage.EMBEDDING) is False
