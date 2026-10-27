"""Rate limiting and progressive delay for sign-in (ADR-0011).

Counters live only in Redis and are changed by single Redis scripts, so parallel requests cannot
slip past a limit between a read and a write. Two mechanisms:

* `hit`: a fixed window per key ("at most N attempts per minute from one address").
* `record_failure` / `retry_after`: a failure counter that makes every further failed attempt wait
  longer (exponential, capped). There is no hard lockout: an attacker who knows an account name must
  not be able to lock its owner out, only to slow guessing down. An operator can `clear` a key.

The login policy built from these parts (`LoginThrottle`) throttles per address, per
account-and-address pair (strict) and per account across all addresses (soft). Identifiers are
hashed before they become Redis keys, so the keys reveal neither account names nor addresses.
Unknown accounts are counted exactly like known ones, so the answer does not reveal which accounts
exist. When Redis does not answer the request fails closed (503): no limiter, no sign-in.
"""

import hashlib
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

# KEYS: failure counter, "wait until" marker.
# ARGV: now (ms), window (s), free failures, base delay (ms), maximum delay (ms).
# The counter's window restarts at every failure. Returns {failures, delay in ms}.
_FAIL = """
local n = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
local free = tonumber(ARGV[3])
local delay = 0
if n > free then
  delay = math.min(tonumber(ARGV[5]), tonumber(ARGV[4]) * 2 ^ (n - free - 1))
end
if delay > 0 then
  local ttl = math.max(tonumber(ARGV[2]), math.ceil(delay / 1000))
  redis.call('SET', KEYS[2], string.format('%d', tonumber(ARGV[1]) + delay), 'EX', ttl)
end
return {n, math.floor(delay)}
"""


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
        self._fail = redis.register_script(_FAIL)

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

    async def record_failure(
        self,
        name: str,
        *,
        window_seconds: int,
        free: int,
        base_ms: int,
        max_ms: int,
    ) -> int:
        """Count a failed attempt. Returns the delay in milliseconds before the next one."""
        try:
            _, delay = await self._fail(
                keys=[self._key("fail", name), self._key("until", name)],
                args=[self._now_ms(), window_seconds, free, base_ms, max_ms],
            )
        except RedisError as error:
            raise DependencyUnavailable from error
        return int(delay)

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
        return (
            f"login:ip:{subject(ip)}",
            f"login:account:{account_part}",
            f"login:pair:{account_part}:{subject(ip)}",
        )

    async def check(self, account: str, ip: str) -> None:
        """Raise 429 when this attempt must wait. Call it BEFORE any expensive password work."""
        ip_name, account_name, pair_name = self._names(account, ip)
        policy = self._policy
        decision = await self._limiter.hit(
            ip_name, limit=policy.ip_limit, window_seconds=policy.ip_window_seconds
        )
        if not decision.allowed:
            raise rate_limited(decision.retry_after)
        wait = max(
            await self._limiter.retry_after(pair_name),
            await self._limiter.retry_after(account_name),
        )
        if wait:
            raise rate_limited(wait)

    async def failed(self, account: str, ip: str) -> None:
        _, account_name, pair_name = self._names(account, ip)
        policy = self._policy
        await self._limiter.record_failure(
            pair_name,
            window_seconds=policy.window_seconds,
            free=policy.pair_free,
            base_ms=policy.pair_base_ms,
            max_ms=policy.pair_max_ms,
        )
        await self._limiter.record_failure(
            account_name,
            window_seconds=policy.window_seconds,
            free=policy.account_free,
            base_ms=policy.account_base_ms,
            max_ms=policy.account_max_ms,
        )

    async def succeeded(self, account: str, ip: str) -> None:
        """A correct password clears the pair counter; the account-wide one fades by itself."""
        _, _, pair_name = self._names(account, ip)
        await self._limiter.clear(pair_name)

    async def unlock(self, account: str) -> int:
        """Operator action: forget every failure and delay recorded for one account."""
        account_part = subject(account)
        removed = await self._limiter.clear_matching(f"login:pair:{account_part}:")
        await self._limiter.clear(f"login:account:{account_part}")
        return removed
