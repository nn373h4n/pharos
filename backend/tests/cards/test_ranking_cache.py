"""The shared ranking of the Home feed without a database (ADR-0027): the person's part of the
score, the certificate of how far the cached list can be trusted, and the cache's behaviour with a
scripted Redis (making it once, waiting, and every way Redis can fail)."""

import asyncio
import json
import random
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from pharos.cards.ranking import (
    INTEREST_POINTS,
    LEVEL_POINTS,
    MAX_BONUS,
    SCORE_SCALE,
    person_score,
    rank_moment,
    scaled_base,
    score_ceiling,
)
from pharos.cards.ranking_cache import (
    KEY_PREFIX,
    Ranking,
    RankingCache,
    _decode,
    _encode,
    key_for,
)
from pharos.cards.repository import Candidate

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


def candidate(base: str, category: str = "tool") -> Candidate:
    return Candidate(id=uuid.uuid4(), category=category, base=Decimal(base))


# --- the person's part of the score -------------------------------------------------------------


def test_the_person_score_adds_the_two_bonuses_to_the_shared_part_and_floors_once() -> None:
    base = Decimal("0.6123456789")
    both = frozenset({"tool"})
    assert person_score(base, "tool", both, both) == 762_345  # 0.6123456789 + 0.10 + 0.05
    assert person_score(base, "tool", both, frozenset()) == 712_345  # interest only
    assert person_score(base, "tool", frozenset(), both) == 662_345  # level only
    assert person_score(base, "news", both, both) == 612_345  # a category nobody asked for
    assert person_score(Decimal("0"), "news", frozenset(), frozenset()) == 0


def test_the_flooring_is_downwards_and_at_the_exact_edge() -> None:
    none: frozenset[str] = frozenset()
    assert person_score(Decimal("0.5000009999"), "x", none, none) == 500_000
    assert person_score(Decimal("0.500001"), "x", none, none) == 500_001
    # The tiniest amount short of a whole step stays below it.
    assert person_score(Decimal("0.4999999999999999999999"), "x", none, none) == 499_999


def test_the_most_a_person_can_add_is_the_two_weights_together() -> None:
    assert Decimal("0.15") == MAX_BONUS
    assert score_ceiling(Decimal("0.5")) == 650_000
    ceiling = score_ceiling(Decimal("0.7"))
    both = frozenset({"tool"})
    # An article with a shared part no higher than that one never beats the ceiling.
    assert person_score(Decimal("0.7"), "tool", both, both) <= ceiling
    assert person_score(Decimal("0.6999"), "tool", both, both) < ceiling
    assert SCORE_SCALE == 1_000_000


def test_a_whole_list_is_certain_throughout_and_a_cut_one_only_above_a_line() -> None:
    whole = Ranking(candidates=[candidate("0.9"), candidate("0.2")], complete=True)
    assert whole.exact_from() is None
    cut = Ranking(candidates=[candidate("0.9"), candidate("0.4")], complete=False)
    assert cut.exact_from() == score_ceiling(Decimal("0.4"))
    assert Ranking(candidates=[], complete=False).exact_from() is None


def test_the_first_page_is_ranked_at_the_start_of_the_five_minutes() -> None:
    moment = rank_moment(datetime(2026, 11, 8, 12, 34, 56, 789_000, tzinfo=UTC))
    assert moment == datetime(2026, 11, 8, 12, 30, 0, tzinfo=UTC)
    on_the_line = datetime(2026, 11, 8, 12, 35, 0, tzinfo=UTC)
    assert rank_moment(on_the_line) == on_the_line


# --- the key and the value ----------------------------------------------------------------------


def test_a_key_names_the_organization_the_profession_and_the_minute_only() -> None:
    moment = datetime(2026, 11, 8, 12, 34, tzinfo=UTC)
    key = key_for(ORG, "accountant", moment)
    assert key == f"{KEY_PREFIX}:{ORG}:accountant:{int(moment.timestamp())}"
    assert key_for(ORG, "sap-basis_2", moment) is not None
    for bad in ("", "Accountant", "a b", "a:b", "a/b", "x" * 65, "-x", "é"):
        assert key_for(ORG, bad, moment) is None, bad


def test_a_value_round_trips_and_every_damaged_one_is_refused() -> None:
    ranking = Ranking(
        candidates=[candidate("0.123456789012345678901", "news"), candidate("0.1")], complete=False
    )
    assert _decode(_encode(ranking)) == ranking
    assert _decode(_encode(Ranking(candidates=[], complete=True))) == Ranking([], True)
    ident = str(uuid.uuid4())
    for bad in (
        "not json",
        "[]",
        '{"c": 1, "e": []}',
        '{"c": true}',
        '{"c": true, "e": "x"}',
        '{"c": true, "e": [["x"]]}',
        f'{{"c": true, "e": [["{ident}", "tool"]]}}',
        f'{{"c": true, "e": [["{ident}", 5, "0.1"]]}}',
        f'{{"c": true, "e": [["{ident}", "tool", 0.5]]}}',
        '{"c": true, "e": [["not-a-uuid", "tool", "0.1"]]}',
        f'{{"c": true, "e": [["{ident}", "tool", "NaNx"]]}}',
        b"\xff\xfe",
    ):
        assert _decode(bad) is None, bad


# --- the cache with a scripted Redis ------------------------------------------------------------


class FakeRedis:
    """Just the four calls the cache makes, with the real meaning of `nx` and `ex`."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.expiry: dict[str, int] = {}
        self.fail: Exception | None = None
        self.reads = 0
        self.keys_read: list[str] = []
        self.delay = 0.0

    async def get(self, key: str) -> Any:
        self.reads += 1
        self.keys_read.append(key)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return self.data.get(key)

    async def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> Any:
        if self.fail:
            raise self.fail
        if nx and key in self.data:
            return None
        self.data[key] = value
        if ex is not None:
            self.expiry[key] = ex
        return True

    async def delete(self, key: str) -> None:
        if self.fail:
            raise self.fail
        self.data.pop(key, None)


def cache_with(redis: FakeRedis, **options: Any) -> RankingCache:
    return RankingCache(redis, **options)  # type: ignore[arg-type]


KEY = f"{KEY_PREFIX}:{ORG}:accountant:1"


@pytest.mark.anyio
async def test_the_first_caller_makes_the_ranking_and_the_next_one_reads_it() -> None:
    redis, calls = FakeRedis(), []

    async def rank(limit: int) -> list[Candidate]:
        calls.append(limit)
        return [candidate("0.9"), candidate("0.8")]

    cache = cache_with(redis, size=5, ttl_seconds=180)
    first = await cache.get_or_rank(KEY, rank)
    second = await cache.get_or_rank(KEY, rank)
    assert calls == [5]  # asked for the size of the cache, and only once
    assert first is not None and second == first
    assert first.complete is True  # two of five: that is every article there is
    assert redis.expiry[KEY] == 180 and f"{KEY}:lock" not in redis.data  # kept, lock let go


@pytest.mark.anyio
async def test_a_list_as_long_as_it_was_asked_for_is_marked_cut_short() -> None:
    redis = FakeRedis()

    async def rank(limit: int) -> list[Candidate]:
        return [candidate("0.5") for _ in range(limit)]

    ranking = await cache_with(redis, size=3).get_or_rank(KEY, rank)
    assert ranking is not None and ranking.complete is False and len(ranking.candidates) == 3


@pytest.mark.anyio
async def test_many_callers_at_once_make_it_once_and_all_get_it() -> None:
    redis, calls = FakeRedis(), []

    async def rank(limit: int) -> list[Candidate]:
        calls.append(1)
        await asyncio.sleep(0.2)
        return [candidate("0.9")]

    cache = cache_with(redis, wait_seconds=2.0)
    answers = await asyncio.gather(*(cache.get_or_rank(KEY, rank) for _ in range(25)))
    assert len(calls) == 1
    assert all(a is not None and len(a.candidates) == 1 for a in answers)


@pytest.mark.anyio
async def test_a_caller_that_waits_too_long_gets_none_and_does_not_rank_again() -> None:
    """Ranking the whole list again, for every waiter, would add the most work when the database
    is busiest: the caller answers its own page with the live query instead."""
    redis, calls = FakeRedis(), []
    redis.data[f"{KEY}:lock"] = "somebody-else"  # somebody holds the lock and never finishes

    async def rank(limit: int) -> list[Candidate]:
        calls.append(1)
        return [candidate("0.9")]

    answer = await cache_with(redis, wait_seconds=0.15).get_or_rank(KEY, rank)
    assert answer is None and calls == []
    assert (
        redis.data[f"{KEY}:lock"] == "somebody-else"
    )  # the other one's lock is not ours to remove


@pytest.mark.anyio
async def test_the_wait_is_measured_in_real_time_so_a_slow_redis_cannot_stretch_it() -> None:
    """Each wait for the ranking holds a database connection. Counting only the sleeps would let a
    Redis that answers slowly keep the request for a minute."""
    import time

    redis = FakeRedis()
    redis.data[f"{KEY}:lock"] = "somebody-else"
    redis.delay = 0.3  # every read takes much longer than the pause between reads

    async def rank(limit: int) -> list[Candidate]:
        return [candidate("0.9")]

    started = time.monotonic()
    assert await cache_with(redis, wait_seconds=0.6).get_or_rank(KEY, rank) is None
    assert time.monotonic() - started < 2.0  # about 1.2 s, not the 60 reads the sleeps alone allow


@pytest.mark.anyio
async def test_a_lock_that_changed_hands_while_we_ranked_is_not_removed() -> None:
    redis = FakeRedis()

    async def rank(limit: int) -> list[Candidate]:
        redis.data[f"{KEY}:lock"] = "a-later-owner"  # ours expired and somebody took it
        return [candidate("0.9")]

    assert await cache_with(redis).get_or_rank(KEY, rank) is not None
    assert redis.data[f"{KEY}:lock"] == "a-later-owner"


@pytest.mark.anyio
async def test_a_ranking_for_a_period_that_is_over_is_read_but_never_made() -> None:
    redis, calls = FakeRedis(), []

    async def rank(limit: int) -> list[Candidate]:
        calls.append(1)
        return [candidate("0.9")]

    cache = cache_with(redis)
    assert await cache.get_or_rank(KEY, rank, make=False) is None
    assert calls == [] and KEY not in redis.data and f"{KEY}:lock" not in redis.data
    made = await cache.get_or_rank(KEY, rank)  # the control: with make it is made ...
    assert made is not None and calls == [1]
    assert await cache.get_or_rank(KEY, rank, make=False) == made  # ... and then read


@pytest.mark.anyio
async def test_the_lock_is_let_go_when_the_ranking_fails() -> None:
    redis = FakeRedis()

    async def rank(limit: int) -> list[Candidate]:
        raise ValueError("the database said no")

    with pytest.raises(ValueError):
        await cache_with(redis).get_or_rank(KEY, rank)
    assert f"{KEY}:lock" not in redis.data


@pytest.mark.anyio
@pytest.mark.parametrize("error", [RedisConnectionError("down"), OSError("reset"), TimeoutError()])
async def test_any_trouble_with_redis_means_no_cache_not_an_error(error: Exception) -> None:
    redis, calls = FakeRedis(), []
    redis.fail = error

    async def rank(limit: int) -> list[Candidate]:
        calls.append(1)
        return []

    assert await cache_with(redis).get_or_rank(KEY, rank) is None
    assert calls == []  # the caller ranks for itself; the cache did not do the work in vain


@pytest.mark.anyio
async def test_a_damaged_value_is_made_again_rather_than_trusted() -> None:
    redis, calls = FakeRedis(), []
    redis.data[KEY] = b"{broken"

    async def rank(limit: int) -> list[Candidate]:
        calls.append(1)
        return [candidate("0.4")]

    answer = await cache_with(redis).get_or_rank(KEY, rank)
    assert answer is not None and calls == [1]
    assert _decode(redis.data[KEY]) is not None  # and what is kept now is sound


# --- F-50: the copy each API process keeps, the kept orders and the stand-in --------------------


class Ticks:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


async def made(redis: FakeRedis, key: str, *bases: str) -> None:
    """A ranking as another process makes it: the value and, next to it, its mark."""
    stamp = uuid.uuid4().hex
    redis.data[key] = _encode(Ranking([candidate(b) for b in bases], complete=True, stamp=stamp))
    redis.data[f"{key}:n"] = stamp


def lists_read(redis: FakeRedis) -> int:
    """How often a whole list was read from Redis (not a mark, not a lock)."""
    return sum(1 for key in redis.keys_read if not key.endswith((":n", ":lock")))


@pytest.mark.anyio
async def test_a_ranking_once_read_is_kept_in_the_process_until_redis_would_have_dropped_it() -> (
    None
):
    redis, ticks = FakeRedis(), Ticks()
    await made(redis, KEY, "0.9", "0.8")
    cache = cache_with(redis, ttl_seconds=900, clock=ticks)
    first = await cache.get_or_rank(KEY, never)
    assert lists_read(redis) == 1
    ticks.value += 899
    assert await cache.get_or_rank(KEY, never) is first and lists_read(redis) == 1  # only the mark
    ticks.value += 2
    assert await cache.get_or_rank(KEY, never) == first and lists_read(redis) == 2  # read again


@pytest.mark.anyio
async def test_a_ranking_this_process_made_is_kept_too() -> None:
    redis = FakeRedis()

    async def rank(limit: int) -> list[Candidate]:
        return [candidate("0.4")]

    cache = cache_with(redis)
    ranking = await cache.get_or_rank(KEY, rank)
    assert ranking is not None and redis.data[f"{KEY}:n"] == ranking.stamp  # its mark is kept
    assert redis.expiry[f"{KEY}:n"] == redis.expiry[KEY]  # and goes with it
    reads = lists_read(redis)
    assert await cache.get_or_rank(KEY, never) is ranking and lists_read(redis) == reads


@pytest.mark.anyio
async def test_the_process_keeps_only_the_last_few_rankings() -> None:
    redis = FakeRedis()
    keys = [f"{KEY}{n}" for n in range(4)]
    for key in keys:
        await made(redis, key, "0.5")
    cache = cache_with(redis, local_entries=3)
    for key in keys:
        await cache.get_or_rank(key, never)
    await cache.get_or_rank(keys[3], never)  # the latest three are kept
    await cache.get_or_rank(keys[1], never)
    assert lists_read(redis) == 4
    await cache.get_or_rank(keys[0], never)  # the oldest was let go
    assert lists_read(redis) == 5


@pytest.mark.anyio
async def test_a_damaged_value_is_not_kept_in_the_process() -> None:
    redis = FakeRedis()
    redis.data[KEY] = "{not json"
    cache = cache_with(redis)
    assert await cache.get_or_rank(KEY, never, make=False) is None
    await made(redis, KEY, "0.7")
    found = await cache.get_or_rank(KEY, never, make=False)
    assert found is not None and len(found.candidates) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["deleted", "mark deleted", "made again"])
async def test_a_ranking_deleted_or_made_again_in_redis_is_read_again(change: str) -> None:
    """Redis decides: forgetting the rankings (an operator, a test) or making one again reaches
    every process at its next request, not when its copy grows old."""
    redis = FakeRedis()
    await made(redis, KEY, "0.9")
    cache = cache_with(redis)
    first = await cache.get_or_rank(KEY, never, make=False)
    assert first is not None
    assert await cache.get_or_rank(KEY, never, make=False) is first  # the control: kept
    if change == "deleted":
        await redis.delete(KEY)
        await redis.delete(f"{KEY}:n")
        assert await cache.get_or_rank(KEY, never, make=False) is None
        return
    if change == "mark deleted":
        await redis.delete(f"{KEY}:n")
    else:
        await made(redis, KEY, "0.2", "0.1")
    found = await cache.get_or_rank(KEY, never, make=False)
    assert found is not None and found is not first
    if change == "made again":
        assert [str(c.base) for c in found.candidates] == ["0.2", "0.1"]


@pytest.mark.anyio
async def test_a_value_without_a_mark_is_used_but_never_trusted_from_the_copy() -> None:
    """A value from before marks existed (an upgrade) is read every time, never served stale."""
    redis = FakeRedis()
    redis.data[KEY] = _encode(Ranking([candidate("0.5")], complete=True))
    cache = cache_with(redis)
    assert await cache.get_or_rank(KEY, never, make=False) is not None
    assert await cache.get_or_rank(KEY, never, make=False) is not None
    assert lists_read(redis) == 2


def test_a_mark_that_is_not_32_hex_digits_is_refused() -> None:
    good = Ranking([candidate("0.5")], complete=True, stamp="a" * 32)
    assert _decode(_encode(good)) == good and _decode(_encode(good)).stamp == "a" * 32  # type: ignore[union-attr]
    for stamp in ["A" * 32, "a" * 31, "a" * 33, 7, ["a"]]:
        raw = _encode(good).replace(f'"{"a" * 32}"', json.dumps(stamp))
        assert _decode(raw) is None, stamp


async def never(limit: int) -> list[Candidate]:
    raise AssertionError("must not rank")


def test_the_floored_shared_part_plus_whole_points_is_exactly_the_person_score() -> None:
    edges = ["0", "0.85", "0.1234565", "0.0000009999999999999999", "0.333333333333333333333333"]
    rng = random.Random(50)  # noqa: S311 (test data, not a secret)
    bases = edges + [str(Decimal(rng.random()) * Decimal("0.85")) for _ in range(300)]
    one = frozenset({"tool"})
    assert (INTEREST_POINTS, LEVEL_POINTS) == (100_000, 50_000)
    for raw in bases:
        base = Decimal(raw)
        shared = scaled_base(base)
        assert shared == person_score(base, "tool", frozenset(), frozenset())
        assert shared + INTEREST_POINTS == person_score(base, "tool", one, frozenset())
        assert shared + LEVEL_POINTS == person_score(base, "tool", frozenset(), one)
        assert shared + INTEREST_POINTS + LEVEL_POINTS == person_score(base, "tool", one, one)


def test_the_kept_order_is_the_order_the_person_score_gives() -> None:
    rng = random.Random(7)  # noqa: S311 (test data, not a secret)
    categories = ["tool", "tutorial", "technique", "news", "research", "case_study", "policy"]
    candidates = [
        Candidate(
            id=uuid.UUID(int=rng.getrandbits(128)),
            category=rng.choice(categories),
            # Few distinct values, so equal scores (broken by the id) are common.
            base=Decimal(rng.randrange(0, 40)) / Decimal(50),
        )
        for _ in range(400)
    ]
    ranking = Ranking(candidates, complete=False)
    for _ in range(30):
        interests = frozenset(rng.sample(categories, rng.randrange(0, 5)))
        levels = frozenset(rng.sample(categories, rng.randrange(0, 4)))
        expected = sorted(
            (person_score(c.base, c.category, interests, levels), c.id) for c in candidates
        )
        assert ranking.order(interests, levels) == expected


def test_an_order_is_a_new_list_and_equality_ignores_what_was_worked_out() -> None:
    ranking = Ranking([candidate("0.5", "tool"), candidate("0.4", "news")], complete=True)
    first = ranking.order(frozenset({"tool"}), frozenset())
    first.clear()  # a caller that changes its order changes nobody else's
    assert len(ranking.order(frozenset({"tool"}), frozenset())) == 2
    assert ranking == Ranking(list(ranking.candidates), complete=True)


def test_an_empty_ranking_has_an_empty_order() -> None:
    assert Ranking([], complete=True).order(frozenset({"tool"}), frozenset()) == []


@pytest.mark.anyio
async def test_while_another_request_makes_the_ranking_the_previous_one_stands_in_at_once() -> None:
    redis = FakeRedis()
    previous = f"{KEY_PREFIX}:{ORG}:accountant:0"
    await made(redis, previous, "0.6")
    redis.data[f"{KEY}:lock"] = "somebody-else"
    cache = cache_with(redis, wait_seconds=5.0)
    started = asyncio.get_running_loop().time()
    found = await cache.lookup(KEY, never, stand_in=previous)
    assert found is not None and found[1] is True and len(found[0].candidates) == 1
    assert asyncio.get_running_loop().time() - started < 1.0  # no wait


@pytest.mark.anyio
async def test_the_stand_in_is_used_only_while_another_request_makes_the_ranking() -> None:
    redis, calls = FakeRedis(), []
    previous = f"{KEY_PREFIX}:{ORG}:accountant:0"
    await made(redis, previous, "0.6")

    async def rank(limit: int) -> list[Candidate]:
        calls.append(limit)
        return [candidate("0.9"), candidate("0.8")]

    cache = cache_with(redis)
    found = await cache.lookup(KEY, rank, stand_in=previous)  # nobody makes it: this one does
    assert found is not None and found[1] is False and len(found[0].candidates) == 2
    assert calls == [cache.size]
    found = await cache.lookup(KEY, rank, stand_in=previous)  # it is there: read, not stood in
    assert found is not None and found[1] is False and len(calls) == 1
    # A period that is over is not made, and nothing stands in for it.
    assert await cache.lookup(f"{KEY}9", rank, make=False, stand_in=previous) is None


@pytest.mark.anyio
async def test_without_a_stand_in_kept_the_caller_waits_as_before() -> None:
    redis = FakeRedis()
    redis.data[f"{KEY}:lock"] = "somebody-else"
    cache = cache_with(redis, wait_seconds=0.15)
    assert await cache.lookup(KEY, never, stand_in=f"{KEY_PREFIX}:{ORG}:accountant:0") is None


def test_a_shared_part_that_is_not_a_number_between_0_and_1_is_refused() -> None:
    """F-50 review: a parsed list stays in the process, so a value that would break the
    arithmetic of every request must not be parsed at all."""
    good = Ranking([candidate("0.5")], complete=True)
    for base in ["NaN", "sNaN", "Infinity", "-Infinity", "-0.0001", "1.0001", "1E+999999"]:
        raw = _encode(good).replace('"0.5"', f'"{base}"')
        assert _decode(raw) is None, base
    for base in ["0", "1", "0.85"]:  # the edges are numbers a ranking can hold
        assert _decode(_encode(good).replace('"0.5"', f'"{base}"')) is not None, base
