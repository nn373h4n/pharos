"""Worker heartbeat shared by the worker (writer) and the API (reader).

The scheduled `pharos.heartbeat` task stores the current time under one Redis key. `/readyz` treats
the worker as alive while that timestamp is recent. A fresh timestamp proves the whole chain works:
beat scheduled the task, the broker delivered it and a worker ran it.
"""

from collections.abc import Awaitable
from typing import Protocol

HEARTBEAT_KEY = "pharos:worker:heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 60
# Three missed beats plus some slack before the worker counts as down.
HEARTBEAT_MAX_AGE_SECONDS = 3 * HEARTBEAT_INTERVAL_SECONDS + 30
# The key expires on its own, so a dead worker does not leave an old value behind forever.
_KEY_TTL_SECONDS = 10 * HEARTBEAT_INTERVAL_SECONDS
# Clock differences between containers on one host are tiny; allow a few seconds into the future.
_CLOCK_SKEW_SECONDS = 5


class SyncRedis(Protocol):
    def set(self, name: str, value: str, ex: int) -> object: ...


class AsyncRedis(Protocol):
    def get(self, name: str) -> Awaitable[bytes | str | None]: ...


def record_heartbeat(client: SyncRedis, now: float) -> None:
    client.set(HEARTBEAT_KEY, repr(now), ex=_KEY_TTL_SECONDS)


async def worker_is_alive(client: AsyncRedis, now: float) -> bool:
    raw = await client.get(HEARTBEAT_KEY)
    if raw is None:
        return False
    try:
        recorded = float(raw)
    except ValueError:
        return False
    return -_CLOCK_SKEW_SECONDS <= now - recorded <= HEARTBEAT_MAX_AGE_SECONDS
