"""Politeness: not more than one request per host in a short time, across all worker processes.

Several sources can live on the same host (a news site with ten feeds). Fetching them all at the
same moment looks like an attack and earns a block, so each fetch first asks the gate for its host.
The gate is one Redis key per host with an expiry; whoever sets it first may go, the others are
told to come back later. The key is under the prefix the worker's Redis user owns.

A host that answers "not now" (a `Retry-After`, or a spent rate limit such as GitHub's) closes its
gate for as long as it asked, at most a day (F-27): the scheduled fetches of every source on that
host and the trial fetches an administrator asks for all wait, instead of only the source whose
fetch heard it.

A redirect can lead a fetch to another host (F-23). The gate of that host is kept too: the fetch
waits there for the usual gap, inside its deadline, and a host that asked to be left alone ends the
fetch with how long it asked for. A "not now" closes the gate of the host that said it, not of the
host the source names.
"""

import asyncio
import hashlib
import math
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pharos.net.safe_http import HOST_HELD, FetchError, FetchErrorKind

KEY_PREFIX = "pharos:worker:host:"
DEFAULT_GAP_SECONDS = 5
MAX_HOLD_SECONDS = 24 * 3600  # as long as the longest backoff of a source (ADR-0012)


class AsyncRedis(Protocol):
    def set(self, name: str, value: str, *, nx: bool = False, px: int) -> Awaitable[Any]: ...
    def pttl(self, name: str) -> Awaitable[Any]: ...


def _name(host: str) -> str:
    return KEY_PREFIX + hashlib.sha256(host.lower().encode()).hexdigest()[:24]


class HostGate:
    def __init__(
        self,
        redis: AsyncRedis,
        gap_seconds: int = DEFAULT_GAP_SECONDS,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._redis = redis
        self.gap_seconds = gap_seconds
        self._sleep = sleep

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

    async def pass_hop(self, host: str) -> None:
        """A fetch already under way is sent to `host` by a redirect: wait until the gate lets it
        in. A gate closed for no longer than the usual gap is waited for (the fetch's deadline still
        runs); one the host closed for longer ends the fetch with FetchError(HOST_HELD) carrying how
        long is left, so the source waits that long like after the host's own answer."""
        while not await self.enter(host):
            wait = await self.wait_seconds(host)
            if wait > self.gap_seconds:
                raise FetchError(FetchErrorKind.BAD_STATUS, HOST_HELD, retry_after=wait)
            await self._sleep(max(wait, 0.1))


class Hops:
    """The hosts one fetch goes through, given to `SafeHttpClient.checking_hops`.

    The first request goes to the source's own host, which passed the gate before the fetch began
    (it is written there in the client's spelling, which may differ from the source's). Every other
    host passes its gate the first time it is met. `answered` is the host a "not now" is to be
    held against: the source's host (`None`), or the host the last redirect led to.
    """

    def __init__(self, gate: HostGate) -> None:
        self._gate = gate
        self._first: str | None = None
        self._entered: set[str] = set()
        self.answered: str | None = None

    async def __call__(self, host: str) -> None:
        if self._first is None:
            self._first = host
            return
        if host == self._first:
            self.answered = None
            return
        self.answered = host
        if host not in self._entered:
            await self._gate.pass_hop(host)
            self._entered.add(host)
