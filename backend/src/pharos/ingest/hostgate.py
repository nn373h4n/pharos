"""Politeness: not more than one request per host in a short time, across all worker processes.

Several sources can live on the same host (a news site with ten feeds). Fetching them all at the
same moment looks like an attack and earns a block, so each fetch first asks the gate for its host.
The gate is one Redis key per host with an expiry; whoever sets it first may go, the others are
told to come back later. The key is under the prefix the worker's Redis user owns.
"""

import hashlib
from collections.abc import Awaitable
from typing import Any, Protocol

KEY_PREFIX = "pharos:worker:host:"
DEFAULT_GAP_SECONDS = 5


class AsyncRedis(Protocol):
    def set(self, name: str, value: str, *, nx: bool, px: int) -> Awaitable[Any]: ...


class HostGate:
    def __init__(self, redis: AsyncRedis, gap_seconds: int = DEFAULT_GAP_SECONDS) -> None:
        self._redis = redis
        self.gap_seconds = gap_seconds

    async def enter(self, host: str) -> bool:
        """True if this fetch may go now; False if the host was fetched from a moment ago."""
        name = KEY_PREFIX + hashlib.sha256(host.lower().encode()).hexdigest()[:24]
        return bool(await self._redis.set(name, "1", nx=True, px=self.gap_seconds * 1000))
