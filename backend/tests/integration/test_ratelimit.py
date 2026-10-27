"""The rate limiter and the login throttle against real Redis."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace

import pytest
from redis.asyncio import Redis

from pharos.core.errors import ApiError, DependencyUnavailable, ErrorCode
from pharos.core.ratelimit import LoginPolicy, LoginThrottle, RateLimiter, network_of, subject
from pharos.core.redis import url_for_database

from ..clock import FrozenClock

pytestmark = pytest.mark.integration

POLICY = LoginPolicy(
    ip_limit=1000,
    ip_window_seconds=60,
    pair_free=2,
    pair_base_ms=2_000,
    pair_max_ms=10_000,
    account_free=4,
    account_base_ms=1_000,
    account_max_ms=5_000,
    window_seconds=600,
)
RESERVE = {"window_seconds": 600, "free": 2, "base_ms": 2_000, "max_ms": 10_000}


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
    assert 0 < await rig.redis.ttl(f"{rig.prefix}hit:expiring") <= 60


@pytest.mark.anyio
async def test_windows_are_independent_per_key(rig: Rig) -> None:
    for _ in range(3):
        await rig.limiter.hit("one", limit=2, window_seconds=60)
    assert (await rig.limiter.hit("two", limit=2, window_seconds=60)).allowed


# --- progressive delay -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_attempts_are_free_at_first_then_the_waits_double_up_to_the_cap(rig: Rig) -> None:
    waits = []
    for _ in range(6):
        assert await rig.limiter.reserve("acct", **RESERVE) == 0
        waits.append(await rig.limiter.retry_after("acct"))
        rig.clock.advance(seconds=60)  # long enough to try again
    assert waits == [0, 0, 2, 4, 8, 10]


@pytest.mark.anyio
async def test_an_attempt_during_a_wait_is_refused_and_not_counted(rig: Rig) -> None:
    for _ in range(3):
        await rig.limiter.reserve("acct", **RESERVE)  # the third sets a 2 s wait
    assert await rig.limiter.reserve("acct", **RESERVE) == 2
    assert await rig.limiter.reserve("acct", **RESERVE) == 2
    assert int(await rig.redis.get(f"{rig.prefix}fail:acct")) == 3
    rig.clock.advance(seconds=1)
    assert await rig.limiter.reserve("acct", **RESERVE) == 1
    rig.clock.advance(seconds=1)
    assert await rig.limiter.reserve("acct", **RESERVE) == 0


@pytest.mark.anyio
async def test_a_parallel_burst_gets_through_only_the_free_attempts(rig: Rig) -> None:
    waits = await asyncio.gather(*(rig.limiter.reserve("race", **RESERVE) for _ in range(50)))
    # The free attempts, plus the one that starts the wait.
    assert sorted(waits).count(0) == RESERVE["free"] + 1
    assert int(await rig.redis.get(f"{rig.prefix}fail:race")) == RESERVE["free"] + 1


@pytest.mark.anyio
async def test_refund_gives_one_attempt_back_but_never_goes_below_zero(rig: Rig) -> None:
    await rig.limiter.reserve("acct", **RESERVE)
    await rig.limiter.reserve("acct", **RESERVE)
    await rig.limiter.refund("acct")
    assert int(await rig.redis.get(f"{rig.prefix}fail:acct")) == 1
    await rig.limiter.refund("acct")
    await rig.limiter.refund("acct")
    await rig.limiter.refund("never-seen")
    assert int(await rig.redis.get(f"{rig.prefix}fail:acct")) == 0
    assert await rig.redis.get(f"{rig.prefix}fail:never-seen") is None


@pytest.mark.anyio
async def test_clear_forgets_attempts_and_waits(rig: Rig) -> None:
    for _ in range(3):
        await rig.limiter.reserve("acct", **RESERVE)
    await rig.limiter.clear("acct")
    assert await rig.limiter.retry_after("acct") == 0
    assert await rig.limiter.reserve("acct", **RESERVE) == 0


@pytest.mark.anyio
async def test_counters_expire_on_their_own(rig: Rig) -> None:
    for _ in range(3):
        await rig.limiter.reserve("acct", **RESERVE)
    for kind in ("fail", "until"):
        assert 0 < await rig.redis.ttl(f"{rig.prefix}{kind}:acct") <= 600


# --- the login policy --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_slow_guesser_is_slowed_down_but_never_locked_out(rig: Rig) -> None:
    ip = "198.51.100.1"
    for _ in range(POLICY.pair_free + 1):
        await rig.throttle.check("alice", ip)  # each of these is a wrong guess
    error = await refused(rig.throttle.check("alice", ip))
    assert error is not None
    assert error.code is ErrorCode.RATE_LIMITED
    assert error.status == 429
    assert error.headers["Retry-After"] == "2"
    rig.clock.advance(seconds=2)
    await rig.throttle.check("alice", ip)  # waiting is enough; no lockout


@pytest.mark.anyio
async def test_the_wait_grows_with_every_wrong_guess(rig: Rig) -> None:
    waits = []
    for _ in range(POLICY.pair_free + 4):
        await rig.throttle.check("alice", "198.51.100.1")
        error = await refused(rig.throttle.check("alice", "198.51.100.1"))
        waits.append(int(error.headers["Retry-After"]) if error else 0)
        rig.clock.advance(seconds=60)
    assert waits == sorted(waits)
    assert waits[0] == 0
    assert waits[-1] == POLICY.pair_max_ms // 1000


@pytest.mark.anyio
async def test_a_parallel_burst_of_guesses_for_one_account_is_cut_off(rig: Rig) -> None:
    results = await asyncio.gather(
        *(refused(rig.throttle.check("alice", "198.51.100.1")) for _ in range(40))
    )
    allowed = [r for r in results if r is None]
    # Not 40 guesses: the free ones plus the one that starts the wait.
    assert len(allowed) == POLICY.pair_free + 1


@pytest.mark.anyio
async def test_a_correct_password_gives_the_attempt_back_and_clears_the_pair(rig: Rig) -> None:
    ip = "198.51.100.1"
    for _ in range(POLICY.pair_free + 1):
        await rig.throttle.check("alice", ip)
        await rig.throttle.check("bob", ip)
    # Positive control: before the success both are being made to wait.
    assert await refused(rig.throttle.check("alice", ip)) is not None
    assert await refused(rig.throttle.check("bob", ip)) is not None
    await rig.throttle.succeeded("alice", ip)
    assert await refused(rig.throttle.check("alice", ip)) is None
    assert await refused(rig.throttle.check("bob", ip)) is not None


@pytest.mark.anyio
async def test_an_attacker_on_one_address_does_not_slow_the_owner_on_another(rig: Rig) -> None:
    for _ in range(3):
        await rig.throttle.check("alice", "203.0.113.50")
    assert await refused(rig.throttle.check("alice", "203.0.113.50")) is not None
    assert await refused(rig.throttle.check("alice", "198.51.100.1")) is None


@pytest.mark.anyio
async def test_guessing_from_many_addresses_is_slowed_by_the_account_wide_counter(
    rig: Rig,
) -> None:
    for number in range(POLICY.account_free + 1):
        await rig.throttle.check("alice", f"203.0.113.{number}")
    error = await refused(rig.throttle.check("alice", "198.51.100.200"))
    assert error is not None
    assert 1 <= int(error.headers["Retry-After"]) <= POLICY.account_max_ms // 1000


@pytest.mark.anyio
async def test_unknown_and_known_accounts_are_throttled_identically(rig: Rig) -> None:
    for name in ("alice", "no-such-user"):
        for _ in range(POLICY.pair_free + 1):
            await rig.throttle.check(name, "198.51.100.1")
    first = await refused(rig.throttle.check("alice", "198.51.100.1"))
    second = await refused(rig.throttle.check("no-such-user", "198.51.100.1"))
    assert first is not None
    assert second is not None
    assert first.headers == second.headers


@pytest.mark.anyio
async def test_attempts_from_one_address_are_capped_whatever_the_account(rig: Rig) -> None:
    strict = LoginThrottle(rig.limiter, replace(POLICY, ip_limit=5))
    results = [await refused(strict.check(f"user{i}", "198.51.100.1")) for i in range(7)]
    assert [r is None for r in results] == [True] * 5 + [False, False]


@pytest.mark.anyio
async def test_an_account_variant_cannot_dodge_the_throttle(rig: Rig) -> None:
    for _ in range(POLICY.pair_free + 1):
        await rig.throttle.check("Alice", "198.51.100.1")
    assert await refused(rig.throttle.check(" alice ", "198.51.100.1")) is not None


@pytest.mark.anyio
async def test_ipv6_clients_are_limited_per_64_not_per_address(rig: Rig) -> None:
    for number in range(POLICY.pair_free + 1):
        await rig.throttle.check("alice", f"2001:db8:1:2::{number + 1}")
    same_64 = await refused(rig.throttle.check("alice", "2001:db8:1:2:aaaa:bbbb:cccc:dddd"))
    assert same_64 is not None  # another address in the same /64 is the same client
    assert await refused(rig.throttle.check("alice", "2001:db8:1:3::1")) is None


def test_network_of_groups_ipv6_and_leaves_the_rest_alone() -> None:
    assert network_of("198.51.100.7") == "198.51.100.7"
    assert network_of("2001:db8:1:2:3:4:5:6") == "2001:db8:1:2::/64"
    assert network_of("::ffff:198.51.100.7") == "198.51.100.7"
    assert network_of("fe80::1%eth0") == "fe80::/64"
    assert network_of("unknown") == "unknown"


@pytest.mark.anyio
async def test_an_operator_can_unlock_an_account(rig: Rig) -> None:
    for address in ("198.51.100.1", "198.51.100.2"):
        for _ in range(POLICY.pair_free + 2):
            await refused(rig.throttle.check("alice", address))
    for _ in range(4):
        await refused(rig.throttle.check("bob", "198.51.100.1"))
    assert await rig.throttle.unlock("alice") >= 2
    assert await refused(rig.throttle.check("alice", "198.51.100.1")) is None
    assert await refused(rig.throttle.check("alice", "198.51.100.2")) is None
    assert await refused(rig.throttle.check("bob", "198.51.100.1")) is not None  # untouched


@pytest.mark.anyio
async def test_no_account_name_or_address_is_stored_in_redis(rig: Rig) -> None:
    await rig.throttle.check("alice@example.com", "198.51.100.77")
    for key in [key async for key in rig.redis.scan_iter(f"{rig.prefix}*")]:
        assert b"alice" not in key
        assert b"198.51.100.77" not in key


def test_subject_is_hashed_stable_and_case_insensitive() -> None:
    assert subject("Alice") == subject(" alice ")
    assert "alice" not in subject("alice")


@pytest.mark.anyio
async def test_when_redis_is_down_the_throttle_fails_closed() -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/1", socket_connect_timeout=0.2, socket_timeout=0.2)
    throttle = LoginThrottle(RateLimiter(dead, FrozenClock()), POLICY)
    try:
        for call in (
            throttle.check("alice", "198.51.100.1"),
            throttle.succeeded("alice", "198.51.100.1"),
            throttle.unlock("alice"),
        ):
            with pytest.raises(DependencyUnavailable):
                await call
    finally:
        await dead.aclose()


@pytest.mark.anyio
async def test_many_successful_sign_ins_never_build_up_a_wait_for_the_account(rig: Rig) -> None:
    # A busy account signing in from many places must not slow itself down.
    for number in range(POLICY.account_free * 3):
        ip = f"203.0.113.{number}"
        await rig.throttle.check("alice", ip)
        await rig.throttle.succeeded("alice", ip)
    assert await rig.limiter.retry_after(f"login:account:{subject('alice')}") == 0
    assert await refused(rig.throttle.check("alice", "198.51.100.250")) is None
