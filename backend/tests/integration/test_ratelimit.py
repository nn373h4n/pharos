"""The rate limiter and the login throttle against real Redis."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace

import pytest
from redis.asyncio import Redis

from pharos.core.errors import ApiError, DependencyUnavailable, ErrorCode
from pharos.core.ratelimit import LoginPolicy, LoginThrottle, RateLimiter
from pharos.core.redis import url_for_database

from ..clock import FrozenClock

pytestmark = pytest.mark.integration

POLICY = LoginPolicy(
    ip_limit=5,
    ip_window_seconds=60,
    pair_free=2,
    pair_base_ms=2_000,
    pair_max_ms=10_000,
    account_free=4,
    account_base_ms=1_000,
    account_max_ms=5_000,
    window_seconds=600,
)


@dataclass
class Rig:
    limiter: RateLimiter
    throttle: LoginThrottle
    redis: Redis
    clock: FrozenClock
    prefix: str


@pytest.fixture
async def rig(redis_url: str) -> AsyncIterator[Rig]:
    redis = Redis.from_url(url_for_database(redis_url, 1))
    clock = FrozenClock()
    prefix = f"test-rl-{uuid.uuid4().hex}:"
    limiter = RateLimiter(redis, clock, prefix=prefix)
    yield Rig(limiter, LoginThrottle(limiter, POLICY), redis, clock, prefix)
    keys = [key async for key in redis.scan_iter(f"{prefix}*")]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()


async def refused(call: object) -> ApiError | None:
    try:
        await call  # type: ignore[misc]
    except ApiError as error:
        return error
    return None


# --- fixed window ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_window_allows_exactly_the_limit(rig: Rig) -> None:
    decisions = [await rig.limiter.hit("a", limit=3, window_seconds=60) for _ in range(5)]
    assert [d.allowed for d in decisions] == [True, True, True, False, False]
    assert decisions[3].retry_after > 0
    assert decisions[0].retry_after == 0


@pytest.mark.anyio
async def test_parallel_hits_cannot_exceed_the_limit(rig: Rig) -> None:
    results = await asyncio.gather(
        *(rig.limiter.hit("burst", limit=10, window_seconds=60) for _ in range(100))
    )
    assert sum(d.allowed for d in results) == 10
    assert max(d.count for d in results) == 100


@pytest.mark.anyio
async def test_the_window_has_an_expiry_so_counters_cannot_pile_up(rig: Rig) -> None:
    await rig.limiter.hit("expiring", limit=1, window_seconds=60)
    key = f"{rig.prefix}hit:expiring"
    assert 0 < await rig.redis.ttl(key) <= 60


@pytest.mark.anyio
async def test_windows_are_independent_per_key(rig: Rig) -> None:
    for _ in range(3):
        await rig.limiter.hit("one", limit=2, window_seconds=60)
    assert (await rig.limiter.hit("two", limit=2, window_seconds=60)).allowed


# --- progressive delay -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_failures_are_free_at_first_then_double_up_to_the_cap(rig: Rig) -> None:
    kwargs = {"window_seconds": 600, "free": 2, "base_ms": 2_000, "max_ms": 10_000}
    delays = [await rig.limiter.record_failure("acct", **kwargs) for _ in range(6)]
    assert delays == [0, 0, 2_000, 4_000, 8_000, 10_000]


@pytest.mark.anyio
async def test_the_wait_is_enforced_until_it_has_passed(rig: Rig) -> None:
    kwargs = {"window_seconds": 600, "free": 0, "base_ms": 5_000, "max_ms": 60_000}
    assert await rig.limiter.retry_after("acct") == 0
    await rig.limiter.record_failure("acct", **kwargs)
    assert await rig.limiter.retry_after("acct") == 5
    rig.clock.advance(seconds=3)
    assert await rig.limiter.retry_after("acct") == 2
    rig.clock.advance(seconds=2)
    assert await rig.limiter.retry_after("acct") == 0


@pytest.mark.anyio
async def test_parallel_failures_are_all_counted(rig: Rig) -> None:
    kwargs = {"window_seconds": 600, "free": 0, "base_ms": 1, "max_ms": 1}
    await asyncio.gather(*(rig.limiter.record_failure("race", **kwargs) for _ in range(50)))
    assert int(await rig.redis.get(f"{rig.prefix}fail:race")) == 50


@pytest.mark.anyio
async def test_parallel_failures_get_distinct_progressive_delays(rig: Rig) -> None:
    kwargs = {"window_seconds": 600, "free": 0, "base_ms": 1_000, "max_ms": 10**9}
    delays = await asyncio.gather(*(rig.limiter.record_failure("seq", **kwargs) for _ in range(8)))
    assert sorted(delays) == [1_000 * 2**i for i in range(8)]


@pytest.mark.anyio
async def test_clear_forgets_failures_and_waits(rig: Rig) -> None:
    kwargs = {"window_seconds": 600, "free": 0, "base_ms": 5_000, "max_ms": 5_000}
    await rig.limiter.record_failure("acct", **kwargs)
    await rig.limiter.clear("acct")
    assert await rig.limiter.retry_after("acct") == 0
    assert await rig.limiter.record_failure("acct", **kwargs) == 5_000  # counting restarted


@pytest.mark.anyio
async def test_failure_counters_expire_on_their_own(rig: Rig) -> None:
    kwargs = {"window_seconds": 600, "free": 0, "base_ms": 60_000, "max_ms": 60_000}
    await rig.limiter.record_failure("acct", **kwargs)
    for kind in ("fail", "until"):
        assert 0 < await rig.redis.ttl(f"{rig.prefix}{kind}:acct") <= 600


# --- the login policy --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_slow_guesser_is_slowed_down_but_never_locked_out(rig: Rig) -> None:
    await rig.throttle.check("alice", "198.51.100.1")
    for _ in range(POLICY.pair_free):
        await rig.throttle.failed("alice", "198.51.100.1")
    await rig.throttle.check("alice", "198.51.100.1")  # still the free attempts
    await rig.throttle.failed("alice", "198.51.100.1")
    error = await refused(rig.throttle.check("alice", "198.51.100.1"))
    assert error is not None
    assert error.code is ErrorCode.RATE_LIMITED
    assert error.status == 429
    assert error.headers["Retry-After"] == "2"
    rig.clock.advance(seconds=2)
    await rig.throttle.check("alice", "198.51.100.1")  # waiting is enough; no lockout


@pytest.mark.anyio
async def test_the_wait_grows_with_every_failure(rig: Rig) -> None:
    # The per-address cap is not what is measured here.
    throttle = LoginThrottle(rig.limiter, replace(POLICY, ip_limit=1000))
    waits = []
    for _ in range(POLICY.pair_free + 4):
        await throttle.failed("alice", "198.51.100.1")
        error = await refused(throttle.check("alice", "198.51.100.1"))
        waits.append(int(error.headers["Retry-After"]) if error else 0)
        rig.clock.advance(seconds=60)
    assert waits == [0, 0, 2, 4, 8, 10]


@pytest.mark.anyio
async def test_a_correct_password_clears_the_pair_but_not_other_people(rig: Rig) -> None:
    throttle = LoginThrottle(rig.limiter, replace(POLICY, ip_limit=1000))
    for _ in range(3):
        await throttle.failed("alice", "198.51.100.1")
        await throttle.failed("bob", "198.51.100.1")
    # Positive control: before the success both are being made to wait.
    assert await refused(throttle.check("alice", "198.51.100.1")) is not None
    assert await refused(throttle.check("bob", "198.51.100.1")) is not None
    await throttle.succeeded("alice", "198.51.100.1")
    assert await refused(throttle.check("alice", "198.51.100.1")) is None
    assert await refused(throttle.check("bob", "198.51.100.1")) is not None


@pytest.mark.anyio
async def test_an_attacker_on_one_address_does_not_slow_the_owner_on_another(rig: Rig) -> None:
    for _ in range(3):
        await rig.throttle.failed("alice", "203.0.113.50")
    assert await refused(rig.throttle.check("alice", "203.0.113.50")) is not None
    assert await refused(rig.throttle.check("alice", "198.51.100.1")) is None


@pytest.mark.anyio
async def test_guessing_from_many_addresses_is_slowed_by_the_account_wide_counter(
    rig: Rig,
) -> None:
    for number in range(POLICY.account_free + 1):
        await rig.throttle.failed("alice", f"203.0.113.{number}")
    # A fresh address (so no pair history) still has to wait, but only for the soft delay.
    error = await refused(rig.throttle.check("alice", "198.51.100.200"))
    assert error is not None
    assert 1 <= int(error.headers["Retry-After"]) <= POLICY.account_max_ms // 1000


@pytest.mark.anyio
async def test_unknown_and_known_accounts_are_throttled_identically(rig: Rig) -> None:
    for name in ("alice", "no-such-user"):
        for _ in range(POLICY.pair_free + 1):
            await rig.throttle.failed(name, "198.51.100.1")
    first = await refused(rig.throttle.check("alice", "198.51.100.1"))
    second = await refused(rig.throttle.check("no-such-user", "198.51.100.1"))
    assert first is not None
    assert second is not None
    assert first.headers == second.headers


@pytest.mark.anyio
async def test_attempts_from_one_address_are_capped_whatever_the_account(rig: Rig) -> None:
    results = [
        await refused(rig.throttle.check(f"user{i}", "198.51.100.1"))
        for i in range(POLICY.ip_limit + 2)
    ]
    assert [r is None for r in results] == [True] * POLICY.ip_limit + [False, False]


@pytest.mark.anyio
async def test_an_operator_can_unlock_an_account(rig: Rig) -> None:
    for address in ("198.51.100.1", "198.51.100.2"):
        for _ in range(POLICY.pair_free + 2):
            await rig.throttle.failed("alice", address)
    await rig.throttle.failed("bob", "198.51.100.1")
    await rig.throttle.failed("bob", "198.51.100.1")
    await rig.throttle.failed("bob", "198.51.100.1")
    assert await rig.throttle.unlock("alice") >= 2
    assert await refused(rig.throttle.check("alice", "198.51.100.1")) is None
    assert await refused(rig.throttle.check("alice", "198.51.100.2")) is None
    assert await refused(rig.throttle.check("bob", "198.51.100.1")) is not None  # untouched


@pytest.mark.anyio
async def test_parallel_wrong_passwords_are_all_counted_by_the_policy(rig: Rig) -> None:
    await asyncio.gather(*(rig.throttle.failed("alice", "198.51.100.1") for _ in range(30)))
    assert int(await rig.redis.get(f"{rig.prefix}fail:login:account:{_subject('alice')}")) == 30


def _subject(account: str) -> str:
    from pharos.core.ratelimit import subject

    return subject(account)


@pytest.mark.anyio
async def test_no_account_name_or_address_is_stored_in_redis(rig: Rig) -> None:
    await rig.throttle.check("alice@example.com", "198.51.100.77")
    await rig.throttle.failed("alice@example.com", "198.51.100.77")
    for key in [key async for key in rig.redis.scan_iter(f"{rig.prefix}*")]:
        assert b"alice" not in key
        assert b"198.51.100.77" not in key


@pytest.mark.anyio
async def test_when_redis_is_down_the_throttle_fails_closed() -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/1", socket_connect_timeout=0.2, socket_timeout=0.2)
    throttle = LoginThrottle(RateLimiter(dead, FrozenClock()), POLICY)
    try:
        for call in (
            throttle.check("alice", "198.51.100.1"),
            throttle.failed("alice", "198.51.100.1"),
            throttle.succeeded("alice", "198.51.100.1"),
            throttle.unlock("alice"),
        ):
            with pytest.raises(DependencyUnavailable):
                await call
    finally:
        await dead.aclose()
