"""The shared ranking of the Home feed without a database (ADR-0027): the person's part of the
score, the certificate of how far the cached list can be trusted, and the cache's behaviour with a
scripted Redis (making it once, waiting, and every way Redis can fail)."""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from pharos.cards.ranking import (
    MAX_BONUS,
    SCORE_SCALE,
    person_score,
    rank_moment,
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
        self.delay = 0.0

    async def get(self, key: str) -> Any:
        self.reads += 1
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
