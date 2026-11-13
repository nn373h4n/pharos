"""Politeness: not more than one request per host in a short time, across all worker processes.

Several sources can live on the same host (a news site with ten feeds). Fetching them all at the
same moment looks like an attack and earns a block, so each fetch first asks the gate for its host.
The gate is one Redis key per host with an expiry; whoever sets it first may go, the others are
told to come back later. The key is under the prefix the worker's Redis user owns.

A host that answers "not now" (a `Retry-After`, or a spent rate limit such as GitHub's) closes its
gate for as long as it asked, at most a day (F-27): the scheduled fetches of every source on that
host and the trial fetches an administrator asks for all wait, instead of only the source whose
fetch heard it.
"""

import hashlib
import math
from collections.abc import Awaitable
from typing import Any, Protocol

KEY_PREFIX = "pharos:worker:host:"
DEFAULT_GAP_SECONDS = 5
MAX_HOLD_SECONDS = 24 * 3600  # as long as the longest backoff of a source (ADR-0012)


class AsyncRedis(Protocol):
    def set(self, name: str, value: str, *, nx: bool = False, px: int) -> Awaitable[Any]: ...
    def pttl(self, name: str) -> Awaitable[Any]: ...


def _name(host: str) -> str:
    return KEY_PREFIX + hashlib.sha256(host.lower().encode()).hexdigest()[:24]


class HostGate:
    def __init__(self, redis: AsyncRedis, gap_seconds: int = DEFAULT_GAP_SECONDS) -> None:
        self._redis = redis
        self.gap_seconds = gap_seconds

    async def enter(self, host: str) -> bool:
        """True if this fetch may go now; False if the host was fetched from a moment ago."""
        return bool(await self._redis.set(_name(host), "1", nx=True, px=self.gap_seconds * 1000))

    async def hold(self, host: str, seconds: int) -> None:
        """The host asked to be left alone for `seconds`: close its gate that long, at most a day.
        A later answer of the same host replaces an earlier one; a wait no longer than the usual
        gap changes nothing."""
        wait = min(seconds, MAX_HOLD_SECONDS)
        if wait > self.gap_seconds:
            await self._redis.set(_name(host), "1", px=wait * 1000)

    async def wait_seconds(self, host: str) -> int:
        """How long the gate of this host stays closed (0 when it is open)."""
        left = await self._redis.pttl(_name(host))
        return math.ceil(left / 1000) if isinstance(left, int) and left > 0 else 0
