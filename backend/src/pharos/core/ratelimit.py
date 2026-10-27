"""Rate limiting and progressive delay for sign-in (ADR-0011).

Counters live only in Redis and are changed by single Redis scripts, so parallel requests cannot
slip past a limit between a read and a write. Two mechanisms:

* `hit`: a fixed window per key ("at most N attempts per minute from one address").
* `reserve` / `refund`: every attempt is booked atomically BEFORE the password is checked and a
  correct password gives it back, so the waits grow with every wrong guess (exponential, capped)
  and a burst of parallel guesses cannot all pass. There is no hard lockout: an attacker who
  knows an account name must not be able to lock its owner out, only to slow guessing down. An
  operator can `clear` a key.

The login policy built from these parts (`LoginThrottle`) throttles per address, per
account-and-address pair (strict) and per account across all addresses (soft). Identifiers are
hashed before they become Redis keys, so the keys reveal neither account names nor addresses.
Unknown accounts are counted exactly like known ones, so the answer does not reveal which accounts
exist. When Redis does not answer the request fails closed (503): no limiter, no sign-in.
"""

import hashlib
import ipaddress
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from pharos.core.clock import Clock
from pharos.core.errors import ApiError, DependencyUnavailable, ErrorCode

KEY_PREFIX = "pharos:rl:"
_MAX_IDENTIFIER = 256

# KEYS: counter. ARGV: window seconds. Returns {count, seconds left}.
_HIT = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {n, redis.call('TTL', KEYS[1])}
"""

# KEYS: attempt counter, "wait until" marker.
# ARGV: now (ms), window (s), free attempts, base delay (ms), maximum delay (ms).
# One atomic step does both the check and the booking: if a wait is still running the attempt is
# refused ({0, ms left}); otherwise it is counted AT ONCE and, after the free attempts, sets the
# wait that the next attempt must respect. Counting before the password is checked is what stops a
# burst of parallel requests from all passing a check that only later failures would have tightened.
# A correct password gives its attempt back (see _REFUND). The counter's window restarts at every
# attempt. Returns {1, 0} when allowed.
_RESERVE = """
local until_ms = redis.call('GET', KEYS[2])
local now = tonumber(ARGV[1])
if until_ms and tonumber(until_ms) > now then
  return {0, tonumber(until_ms) - now}
end
local n = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
local free = tonumber(ARGV[3])
if n > free then
  local delay = math.min(tonumber(ARGV[5]), tonumber(ARGV[4]) * 2 ^ (n - free - 1))
  local ttl = math.max(tonumber(ARGV[2]), math.ceil(delay / 1000))
  redis.call('SET', KEYS[2], string.format('%d', now + delay), 'EX', ttl)
end
return {1, 0}
"""

# KEYS: attempt counter. Gives one booked attempt back, never below zero.
_REFUND = """
local n = tonumber(redis.call('GET', KEYS[1]) or '0')
if n > 0 then redis.call('DECR', KEYS[1]) end
return n
"""


def network_of(address: str) -> str:
    """The unit a client is limited as: an IPv4 address, or the /64 of an IPv6 address.

    A single IPv6 customer holds a whole /64, so limiting single addresses would let one machine
    rotate through billions of them. Anything that is not an IP address is used as it is.
    """
    try:
        parsed = ipaddress.ip_address(address.strip().split("%")[0])
    except ValueError:
        return address
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped:
            return str(parsed.ipv4_mapped)
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return str(parsed)


def subject(*parts: str) -> str:
    """A Redis-safe key part for account names, addresses and the like."""
    joined = "\x1f".join(part.strip().lower()[:_MAX_IDENTIFIER] for part in parts)
    return hashlib.sha256(joined.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    count: int
    retry_after: int  # seconds; 0 when allowed


def rate_limited(retry_after: int) -> ApiError:
    return ApiError(ErrorCode.RATE_LIMITED, 429, headers={"Retry-After": str(max(1, retry_after))})


class RateLimiter:
    def __init__(self, redis: Redis, clock: Clock, prefix: str = KEY_PREFIX) -> None:
        self._redis = redis
        self._clock = clock
        self._prefix = prefix
        self._hit = redis.register_script(_HIT)
        self._reserve_script = redis.register_script(_RESERVE)
        self._refund_script = redis.register_script(_REFUND)

    def _key(self, kind: str, name: str) -> str:
        return f"{self._prefix}{kind}:{name}"

    def _now_ms(self) -> int:
        return int(self._clock.now().timestamp() * 1000)

    async def hit(self, name: str, *, limit: int, window_seconds: int) -> Decision:
        """Count one attempt. `allowed` is False once more than `limit` happened in the window."""
        try:
            count, ttl = await self._hit(keys=[self._key("hit", name)], args=[window_seconds])
        except RedisError as error:
            raise DependencyUnavailable from error
        allowed = int(count) <= limit
        return Decision(allowed, int(count), 0 if allowed else max(1, int(ttl)))

    async def reserve(
        self,
        name: str,
        *,
        window_seconds: int,
        free: int,
        base_ms: int,
        max_ms: int,
    ) -> int:
        """Book one attempt. Returns 0 when it may go ahead, else the seconds it must wait."""
        try:
            allowed, wait_ms = await self._reserve_script(
                keys=[self._key("fail", name), self._key("until", name)],
                args=[self._now_ms(), window_seconds, free, base_ms, max_ms],
            )
        except RedisError as error:
            raise DependencyUnavailable from error
        return 0 if int(allowed) else max(1, -(-int(wait_ms) // 1000))

    async def refund(self, name: str) -> None:
        """Give back one booked attempt (the password turned out to be right)."""
        try:
            await self._refund_script(keys=[self._key("fail", name)])
        except RedisError as error:
            raise DependencyUnavailable from error

    async def retry_after(self, name: str) -> int:
        """Seconds the caller must still wait (0 = may try)."""
        try:
            raw: Any = await self._redis.get(self._key("until", name))
        except RedisError as error:
            raise DependencyUnavailable from error
        if raw is None:
            return 0
        remaining_ms = int(raw) - self._now_ms()
        return 0 if remaining_ms <= 0 else -(-remaining_ms // 1000)

    async def clear(self, name: str) -> None:
        try:
            await self._redis.delete(
                self._key("fail", name), self._key("until", name), self._key("hit", name)
            )
        except RedisError as error:
            raise DependencyUnavailable from error

    async def clear_matching(self, pattern: str) -> int:
        """Delete every counter whose name starts with `pattern` (operator unlock)."""
        removed = 0
        try:
            for kind in ("fail", "until", "hit"):
                async for key in self._redis.scan_iter(self._key(kind, f"{pattern}*")):
                    removed += await self._redis.delete(key)
        except RedisError as error:
            raise DependencyUnavailable from error
        return removed


@dataclass(frozen=True, slots=True)
class LoginPolicy:
    # Attempts of any kind from one address.
    ip_limit: int = 30
    ip_window_seconds: int = 60
    # One account from one address: strict.
    pair_free: int = 3
    pair_base_ms: int = 2_000
    pair_max_ms: int = 300_000
    # One account from all addresses together: soft, so it cannot lock the owner out for long.
    account_free: int = 10
    account_base_ms: int = 1_000
    account_max_ms: int = 60_000
    # Failures are forgotten this long after the last one.
    window_seconds: int = 900


class LoginThrottle:
    def __init__(self, limiter: RateLimiter, policy: LoginPolicy | None = None) -> None:
        self._limiter = limiter
        self._policy = policy or LoginPolicy()

    @staticmethod
    def _names(account: str, ip: str) -> tuple[str, str, str]:
        account_part = subject(account)
        source = subject(network_of(ip))
        return (
            f"login:ip:{source}",
            f"login:account:{account_part}",
            f"login:pair:{account_part}:{source}",
        )

    async def check(self, account: str, ip: str) -> None:
        """Book this attempt, or raise 429 when it must wait.

        Call it BEFORE any expensive password work. The attempt is counted here, not after the
        password has been checked, so parallel requests cannot all slip through before the first
        failure is recorded. `succeeded` gives the attempt back.
        """
        ip_name, account_name, pair_name = self._names(account, ip)
        policy = self._policy
        decision = await self._limiter.hit(
            ip_name, limit=policy.ip_limit, window_seconds=policy.ip_window_seconds
        )
        if not decision.allowed:
            raise rate_limited(decision.retry_after)
        wait = await self._limiter.reserve(
            pair_name,
            window_seconds=policy.window_seconds,
            free=policy.pair_free,
            base_ms=policy.pair_base_ms,
            max_ms=policy.pair_max_ms,
        )
        if wait:
            raise rate_limited(wait)
        wait = await self._limiter.reserve(
            account_name,
            window_seconds=policy.window_seconds,
            free=policy.account_free,
            base_ms=policy.account_base_ms,
            max_ms=policy.account_max_ms,
        )
        if wait:
            await self._limiter.refund(pair_name)  # this attempt never reached the password
            raise rate_limited(wait)

    async def succeeded(self, account: str, ip: str) -> None:
        """A correct password gives its attempt back and clears the pair's counter."""
        _, account_name, pair_name = self._names(account, ip)
        await self._limiter.clear(pair_name)
        await self._limiter.refund(account_name)

    async def unlock(self, account: str) -> int:
        """Operator action: forget every failure and delay recorded for one account."""
        account_part = subject(account)
        removed = await self._limiter.clear_matching(f"login:pair:{account_part}:")
        await self._limiter.clear(f"login:account:{account_part}")
        return removed
