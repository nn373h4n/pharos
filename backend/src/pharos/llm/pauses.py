"""A stage that keeps failing rests for a minute (ADR-0016, F-40 c).

After five failures in a row, a stage (filter, enrichment, insight, embedding, the key check) is not
called for 60 seconds, so an outage of the provider does not turn into a loop of paid or slow calls.
The worker builds a new gateway for every task, so a pause kept in the gateway object was forgotten
at once and every task still tried up to three times. The count and the pause therefore live in
Redis, under the worker's own prefix, and every task of every worker process sees them. If Redis
cannot be reached, the process falls back to its own count: the budget, which needs Redis too, stops
the calls anyway.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pharos.llm.types import Stage

logger = logging.getLogger(__name__)

AFTER = 5  # failures in a row
SECONDS = 60.0  # how long the stage rests
FAILURE_WINDOW_SECONDS = 600  # a count with no new failure for this long starts again
KEY_PREFIX = "pharos:worker:llm:"


class Pauses(Protocol):
    async def paused(self, stage: Stage) -> bool: ...
    async def failed(self, stage: Stage) -> None: ...
    async def worked(self, stage: Stage) -> None: ...


@dataclass(slots=True)
class _Count:
    failures: int = 0
    until: float = 0.0


class LocalPauses:
    """One process's view: enough for a single long-lived gateway (tests, the API)."""

    def __init__(
        self,
        monotonic: Callable[[], float] = time.monotonic,
        *,
        after: int = AFTER,
        seconds: float = SECONDS,
    ) -> None:
        self._monotonic = monotonic
        self._after = after
        self._seconds = seconds
        self._counts: dict[Stage, _Count] = {}

    def _count(self, stage: Stage) -> _Count:
        return self._counts.setdefault(stage, _Count())

    async def paused(self, stage: Stage) -> bool:
        return self._count(stage).until > self._monotonic()

    async def failed(self, stage: Stage) -> None:
        count = self._count(stage)
        count.failures += 1
        if count.failures >= self._after:
            count.until = self._monotonic() + self._seconds
            count.failures = 0

    async def worked(self, stage: Stage) -> None:
        self._count(stage).failures = 0


class AsyncRedis(Protocol):
    def exists(self, *names: str) -> Awaitable[Any]: ...
    def incr(self, name: str) -> Awaitable[Any]: ...
    def expire(self, name: str, time: int) -> Awaitable[Any]: ...
    def set(self, name: str, value: str, *, px: int) -> Awaitable[Any]: ...
    def delete(self, *names: str) -> Awaitable[Any]: ...


def pause_key(stage: Stage) -> str:
    return f"{KEY_PREFIX}pause:{stage.value}"


def failures_key(stage: Stage) -> str:
    return f"{KEY_PREFIX}failures:{stage.value}"


class RedisPauses:
    """The count and the pause shared by every task and process of the worker."""

    def __init__(
        self,
        redis: AsyncRedis,
        *,
        after: int = AFTER,
        seconds: float = SECONDS,
        fallback: Pauses | None = None,
    ) -> None:
        self._redis = redis
        self._after = after
        self._seconds = seconds
        self._fallback = fallback or LocalPauses(after=after, seconds=seconds)

    async def paused(self, stage: Stage) -> bool:
        try:
            return bool(await self._redis.exists(pause_key(stage)))
        except Exception:
            logger.warning("llm: the pause could not be read", extra={"stage": stage.value})
            return await self._fallback.paused(stage)

    async def failed(self, stage: Stage) -> None:
        try:
            failures = int(await self._redis.incr(failures_key(stage)))
            await self._redis.expire(failures_key(stage), FAILURE_WINDOW_SECONDS)
            if failures >= self._after:
                await self._redis.set(pause_key(stage), "1", px=int(self._seconds * 1000))
                await self._redis.delete(failures_key(stage))
                logger.warning("llm: a stage rests after failing", extra={"stage": stage.value})
        except Exception:
            logger.warning("llm: the failure could not be counted", extra={"stage": stage.value})
            await self._fallback.failed(stage)

    async def worked(self, stage: Stage) -> None:
        try:
            await self._redis.delete(failures_key(stage))
        except Exception:
            await self._fallback.worked(stage)
