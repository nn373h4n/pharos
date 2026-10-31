"""The money and the pace of model calls (ADR-0016).

A call books its estimated cost against the day's counter BEFORE it is made, in one atomic Redis
step, and settles to the real cost afterwards. So parallel calls cannot all pass a check that only
their own spending would have tightened, and a bug or a hostile feed cannot spend more than the cap
plus one estimate error. If Redis does not answer, nothing is called: no counter, no spending.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from pharos.core.clock import Clock
from pharos.llm.errors import BudgetUnavailable

# Under the worker's own namespace, the keys its Redis user may use (deploy/redis/start.sh).
KEY_PREFIX = "pharos:worker:llm:"
DAY_SECONDS = 2 * 24 * 3600  # a day's keys outlive the day a little, so a late settle still lands
WARN_PERCENT = 80

# KEYS: spent, warned-today marker, exhausted-today marker. ARGV: estimate, cap, ttl seconds.
# Returns {allowed, total, warned_first, exhausted_first}. Refused calls change nothing.
_RESERVE = """
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
local est = tonumber(ARGV[1])
local cap = tonumber(ARGV[2])
if cur + est > cap then
  local first = redis.call('SET', KEYS[3], '1', 'NX', 'EX', ARGV[3])
  return {0, cur, 0, first and 1 or 0}
end
local total = redis.call('INCRBY', KEYS[1], est)
redis.call('EXPIRE', KEYS[1], ARGV[3])
local warned = 0
if total * 100 >= cap * 80 then
  if redis.call('SET', KEYS[2], '1', 'NX', 'EX', ARGV[3]) then warned = 1 end
end
return {1, total, warned, 0}
"""

# KEYS: spent. ARGV: change (may be negative), ttl. Never goes below zero.
_ADJUST = """
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
local n = cur + tonumber(ARGV[1])
if n < 0 then n = 0 end
redis.call('SET', KEYS[1], tostring(n))
redis.call('EXPIRE', KEYS[1], ARGV[2])
return n
"""

# KEYS: counter of this minute. ARGV: limit. Returns {allowed, count}.
_PACE = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], 120) end
return {n <= tonumber(ARGV[1]) and 1 or 0, n}
"""


def spent_key(org: UUID, day: str) -> str:
    """The key of an organization's counter for a day (`day` as YYYYMMDD, UTC). The API reads it for
    the admin page; only the worker writes it."""
    return f"{KEY_PREFIX}spent:{org}:{day}"


@dataclass(frozen=True, slots=True)
class Reservation:
    org: UUID
    key: str
    amount: int
    total_after: int
    warned_first: bool  # the 80% line was crossed by this call, for the first time today


@dataclass(frozen=True, slots=True)
class Refusal:
    exhausted_first: bool  # the first refusal today: worth one audit entry and one banner


class Budget:
    def __init__(self, redis: Redis, clock: Clock, *, daily_cap_micro_usd: int) -> None:
        self._redis = redis
        self._clock = clock
        self._cap = daily_cap_micro_usd
        self._reserve_script = redis.register_script(_RESERVE)
        self._adjust_script = redis.register_script(_ADJUST)
        self._pace_script = redis.register_script(_PACE)

    @property
    def cap(self) -> int:
        return self._cap

    def _day(self) -> str:
        return self._clock.now().strftime("%Y%m%d")

    def _spent_key(self, org: UUID) -> str:
        return spent_key(org, self._day())

    async def reserve(self, org: UUID, estimate: int) -> Reservation | Refusal:
        """Book `estimate` micro-dollars, or refuse. Raises BudgetUnavailable if Redis fails."""
        key = self._spent_key(org)
        try:
            allowed, total, flag, first = await self._reserve_script(
                keys=[key, f"{key}:warned", f"{key}:exhausted"],
                args=[max(0, estimate), self._cap, DAY_SECONDS],
            )
        except RedisError as error:
            raise BudgetUnavailable() from error
        if int(allowed) != 1:
            return Refusal(exhausted_first=bool(int(first)))
        return Reservation(org, key, max(0, estimate), int(total), bool(int(flag)))

    async def settle(self, reservation: Reservation, actual: int) -> None:
        """Replace the estimate by what the call really cost."""
        await self._adjust(reservation.key, max(0, actual) - reservation.amount)

    async def release(self, reservation: Reservation) -> None:
        """Give the estimate back: the call did not happen."""
        await self._adjust(reservation.key, -reservation.amount)

    async def _adjust(self, key: str, change: int) -> None:
        try:
            await self._adjust_script(keys=[key], args=[change, DAY_SECONDS])
        except RedisError as error:
            raise BudgetUnavailable() from error

    async def spent(self, org: UUID) -> int:
        """What is booked today (for the admin page and for tests)."""
        try:
            value: Any = await self._redis.get(self._spent_key(org))
        except RedisError as error:
            raise BudgetUnavailable() from error
        return int(value or 0)

    async def pace(self, org: UUID, calls_per_minute: int) -> bool:
        """Count one call in this minute; False when the minute's limit is passed."""
        minute = self._clock.now().strftime("%Y%m%d%H%M")
        try:
            allowed, _ = await self._pace_script(
                keys=[f"{KEY_PREFIX}pace:{org}:{minute}"], args=[calls_per_minute]
            )
        except RedisError as error:
            raise BudgetUnavailable() from error
        return int(allowed) == 1
