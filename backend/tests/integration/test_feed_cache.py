"""The shared ranking of the Home feed (ADR-0027, F-49): the cached feed must give exactly the pages
the live query gives, page by page and cursor by cursor, and the cache must never show what may not
be shown, hold a person, or cross an organization. Real PostgreSQL and Redis."""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from pharos.cards import repository
from pharos.cards.ranking_cache import KEY_PREFIX, RankingCache

from .test_cards_api import card, feed, stack_and_person, titles
from .test_insights_api import set_profile
from .test_local_auth import MakeStack, Stack, make_stack  # noqa: F401
from .test_sources_api import _remove_what_the_test_made  # noqa: F401
from .test_user_admin import Person, person

pytestmark = pytest.mark.integration

CATEGORIES = ["tool", "tutorial", "technique", "news", "research", "case_study"]


class Fixed:
    """A clock that stands still, two and a half minutes into a ranking period, so no test crosses
    a boundary."""

    def __init__(self) -> None:
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        self.value = now.replace(minute=now.minute // 5 * 5) + timedelta(seconds=150)

    def now(self) -> datetime:
        return self.value


class Cards:
    """The service of the running app, with the cache and the clock the test chooses."""

    def __init__(self, stack: Stack) -> None:
        self.service: Any = stack.app.state.cards
        self.redis = Redis.from_url(stack.settings.redis_url.get_secret_value())
        self.clock = Fixed()
        self.service._clock = self.clock

    def live(self) -> None:
        self.service._cache = None

    def cached(self, size: int = 500) -> RankingCache:
        self.service._cache = RankingCache(self.redis, size=size)
        return self.service._cache  # type: ignore[no-any-return]

    async def clear(self) -> None:
        async for key in self.redis.scan_iter(match=f"{KEY_PREFIX}:*"):
            await self.redis.delete(key)

    async def keys(self) -> list[str]:
        """The rankings and locks kept; each ranking's 32-byte mark (F-50) goes with it and is
        checked here rather than counted."""
        found = [
            key.decode() if isinstance(key, bytes) else key
            async for key in self.redis.scan_iter(match=f"{KEY_PREFIX}:*")
        ]
        rankings = [k for k in found if not k.endswith((":n", ":lock"))]
        assert sorted(k for k in found if k.endswith(":n")) == sorted(f"{k}:n" for k in rankings)
        return [k for k in found if not k.endswith(":n")]

    async def close(self) -> None:
        await self.service.wait_for_rankings()  # nothing made in the background outlives its Redis
        await self.redis.aclose()


async def pages(who: Person, per_page: int = 20) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = await feed(who, limit=per_page, **({"cursor": cursor} if cursor else {}))
        found.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return found


async def fill(stack: Stack, count: int = 120) -> list[uuid.UUID]:
    """Articles with every kind of difference the score has: fit, quality, age, category, and
    clusters of copies."""
    made: list[uuid.UUID] = []
    for n in range(count):
        made.append(
            await card(
                stack,
                title=f"Story {n:03d}",
                hours=1 + (n * 7) % 600,
                category=CATEGORIES[n % len(CATEGORIES)],
                quality=1 + n % 5,
                fit={"accountant": (n * 37) % 101} if n % 4 else None,
            )
        )
    async with stack.owner.begin() as connection:
        for n in range(0, count - 2, 6):  # copies of one story: three articles share a cluster
            await connection.execute(
                text("UPDATE items SET cluster_id = :c WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"c": uuid.uuid4(), "ids": made[n : n + 3]},
            )
    return made


async def mark(stack: Stack, who: Person, item: uuid.UUID, kind: str) -> None:
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO interactions (org_id, user_id, item_id, type) "
                "VALUES (:o, :u, :i, :t) ON CONFLICT DO NOTHING"
            ),
            {"o": stack.org_a, "u": who.id, "i": item, "t": kind},
        )


async def interests(stack: Stack, who: Person, level: str | None, wants: list[str]) -> None:
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "UPDATE user_profiles SET ai_level = :l, interests = CAST(:i AS jsonb) "
                "WHERE user_id = :u"
            ),
            {"l": level, "i": json.dumps(wants), "u": who.id},
        )


@pytest.mark.anyio
@pytest.mark.parametrize("size", [500, 40, 1])
async def test_the_cached_feed_is_the_live_feed_page_by_page(
    make_stack: MakeStack, size: int
) -> None:
    """A full list, a list cut short (the live query takes over past it) and a list of one."""
    stack, who = await stack_and_person(make_stack, level="advanced", interests=["research"])
    cards = Cards(stack)
    try:
        made = await fill(stack)
        for item in made[:6]:  # the best articles are among the ones this person hid or saved
            await mark(stack, who, item, "not_useful")
        for item in made[6:12]:
            await mark(stack, who, item, "save")
        await cards.clear()
        cards.live()
        expected = await pages(who)
        assert sum(len(p["items"]) for p in expected) > 40  # the control: a long feed to compare
        cards.cached(size)
        got = await pages(who)
        assert got == expected
        assert await pages(who, per_page=7) == await live_pages(cards, who, per_page=7)
    finally:
        await cards.close()


async def live_pages(cards: Cards, who: Person, per_page: int) -> list[dict[str, Any]]:
    cached = cards.service._cache
    cards.live()
    try:
        return await pages(who, per_page=per_page)
    finally:
        cards.service._cache = cached


@pytest.mark.anyio
async def test_people_with_other_interests_share_one_ranking_and_each_gets_their_own_order(
    make_stack: MakeStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, first = await stack_and_person(make_stack, level="beginner", interests=["writing"])
    second = await person(stack, "member")
    await set_profile(stack, second, profession="accountant")
    await interests(stack, second, "advanced", ["research", "planning"])
    cards = Cards(stack)
    try:
        await fill(stack)
        await cards.clear()
        cards.live()
        want_first, want_second = await pages(first), await pages(second)
        assert titles(want_first[0]) != titles(want_second[0])  # the control: orders do differ
        cards.cached()
        ranked: list[int] = []
        original = repository.ranked_candidates

        async def counted(*args: Any, **kwargs: Any) -> Any:
            ranked.append(1)
            return await original(*args, **kwargs)

        monkeypatch.setattr(repository, "ranked_candidates", counted)
        assert await pages(first) == want_first
        assert await pages(second) == want_second
        assert len(ranked) == 1  # one ranking for both people, however many pages they read
        cards.clock.value += timedelta(seconds=300)  # the next period is ranked again ...
        await feed(first)
        await cards.service.wait_for_rankings()  # ... after the page, in the background (F-61)
        assert len(ranked) == 2
    finally:
        await cards.close()


@pytest.mark.anyio
async def test_a_hold_a_hide_or_a_deletion_takes_effect_at_once_though_the_ranking_is_kept(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    cards = Cards(stack)
    try:
        made = await fill(stack, 30)
        await cards.clear()
        cards.cached()
        before = titles(await feed(who, limit=50))
        assert {"Story 003", "Story 004", "Story 005"} <= set(before)  # not copies of one story
        async with stack.owner.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE items SET status = 'held', hold_reason = 'link_ip_host' WHERE id = :i"
                ),
                {"i": made[3]},
            )
            await connection.execute(
                text("UPDATE items SET status = 'hidden' WHERE id = :i"), {"i": made[4]}
            )
            await connection.execute(text("DELETE FROM items WHERE id = :i"), {"i": made[5]})
        after = titles(await feed(who, limit=50))  # the same period: the ranking is the kept one
        assert not {"Story 003", "Story 004", "Story 005"} & set(after)
        assert len(after) == len(before) - 3  # and nothing else went missing
    finally:
        await cards.close()


@pytest.mark.anyio
async def test_when_redis_does_not_answer_the_feed_is_the_same_only_slower(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack, level="intermediate", interests=["coding"])
    cards = Cards(stack)
    try:
        await fill(stack, 60)
        cards.live()
        expected = await pages(who)
        dead = Redis.from_url(
            "redis://127.0.0.1:1/0", socket_timeout=0.2, socket_connect_timeout=0.2
        )
        cards.service._cache = RankingCache(dead)
        assert await pages(who) == expected
        await dead.aclose()
    finally:
        await cards.close()


@pytest.mark.anyio
async def test_the_kept_ranking_holds_no_person_and_one_organization_never_sees_the_other(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    other = await person(
        stack,
        "member",
    )
    cards = Cards(stack)
    try:
        await fill(stack, 20)
        await card(stack, title="Theirs", org=stack.org_b, fit={"accountant": 100})
        await cards.clear()
        cards.cached()
        await mark(stack, who, (await _first(stack)), "not_useful")
        assert "Theirs" not in titles(await feed(who))
        keys = await cards.keys()
        assert len(keys) == 1 and str(stack.org_a) in keys[0]
        raw = await cards.redis.get(keys[0])
        assert raw is not None
        stored = raw.decode() if isinstance(raw, bytes) else raw
        # Articles and numbers only: nothing of any person, no interaction, no text of an article.
        assert str(who.id) not in stored and str(other.id) not in stored
        assert "Story" not in stored and "Summary" not in stored
        assert json.loads(stored)["c"] is True
        assert 0 < await cards.redis.ttl(keys[0]) <= 900
    finally:
        await cards.close()


async def _first(stack: Stack) -> uuid.UUID:
    async with stack.owner.connect() as connection:
        found = (
            await connection.execute(
                text("SELECT id FROM items WHERE org_id = :o ORDER BY id LIMIT 1"),
                {"o": stack.org_a},
            )
        ).scalar_one()
        return uuid.UUID(str(found))


@pytest.mark.anyio
async def test_a_damaged_value_in_redis_is_ignored_and_made_again(make_stack: MakeStack) -> None:
    stack, who = await stack_and_person(make_stack)
    cards = Cards(stack)
    try:
        await fill(stack, 20)
        cards.live()
        expected = await pages(who)
        await cards.clear()
        cache = cards.cached()
        assert cache is not None
        await feed(who)
        key = (await cards.keys())[0]
        for broken in (b"not json", b'{"c": true, "e": [["x"]]}', b'{"c": 1, "e": []}', b"[]"):
            await cards.redis.set(key, broken, ex=60)
            assert await pages(who) == expected
    finally:
        await cards.close()


@pytest.mark.anyio
async def test_a_forged_cursor_cannot_fill_redis_with_rankings(make_stack: MakeStack) -> None:
    """Cursors are not signed. Any moment a member names would be a ranking of its own, kept in
    Redis for fifteen minutes; only moments the feed itself ranks at are accepted, and an old one
    is only read, never made."""
    stack, who = await stack_and_person(make_stack)
    cards = Cards(stack)
    try:
        made = await fill(stack, 30)
        await cards.clear()
        cards.cached()
        first = await feed(who, limit=5)
        assert len(await cards.keys()) == 1 and first["next_cursor"]
        raw = first["next_cursor"]
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        base = datetime.fromisoformat(body["a"])
        for seconds in range(1, 40):  # thirty-nine forged moments, one second apart
            forged = dict(body, a=(base - timedelta(seconds=seconds)).isoformat())
            cursor = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
            answer = await who.client.get("/api/cards", params={"cursor": cursor, "limit": 5})
            assert answer.status_code == 422 and "invalid_cursor" in answer.text
        assert len(await cards.keys()) == 1  # nothing was made for any of them
        # An honest cursor of an older period is served without making a ranking for it.
        older = dict(body, a=(base - timedelta(minutes=15)).isoformat())
        cursor = base64.urlsafe_b64encode(json.dumps(older).encode()).decode().rstrip("=")
        answer = await who.client.get("/api/cards", params={"cursor": cursor, "limit": 5})
        assert answer.status_code == 200
        assert len(await cards.keys()) == 1
        assert made  # (the articles exist; the feed above had something to page through)
    finally:
        await cards.close()


async def forged(first: dict[str, Any], moment: datetime) -> str:
    """A cursor of the real kind, but naming another (aligned) moment."""
    raw = first["next_cursor"]
    body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    body["a"] = moment.isoformat()
    return base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")


@pytest.mark.anyio
async def test_a_ranking_is_made_for_the_previous_period_and_not_for_the_one_before_it(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    cards = Cards(stack)
    try:
        await fill(stack, 30)
        await cards.clear()
        cards.cached()
        first = await feed(who, limit=5)
        assert len(await cards.keys()) == 1
        period = datetime.fromisoformat(
            json.loads(
                base64.urlsafe_b64decode(
                    first["next_cursor"] + "=" * (-len(first["next_cursor"]) % 4)
                )
            )["a"]
        )
        # Exactly one period back: still made (a visit that began a little before the boundary).
        cursor = await forged(first, period - timedelta(minutes=5))
        assert (
            await who.client.get("/api/cards", params={"cursor": cursor, "limit": 5})
        ).status_code == 200
        assert len(await cards.keys()) == 2
        # Two periods back: only read, never made.
        cursor = await forged(first, period - timedelta(minutes=10))
        assert (
            await who.client.get("/api/cards", params={"cursor": cursor, "limit": 5})
        ).status_code == 200
        assert len(await cards.keys()) == 2
    finally:
        await cards.close()


@pytest.mark.anyio
@pytest.mark.parametrize("size", [3, 6, 9, 14])
async def test_articles_with_exactly_the_same_score_are_paged_like_the_live_query(
    make_stack: MakeStack, size: int
) -> None:
    """Fourteen articles equal in fit, quality, category and time: the order among them is by id,
    and the cut of the kept list runs through them. Their score is also exactly the ceiling a cut
    list can prove, which is the edge of the certain part."""
    stack, who = await stack_and_person(make_stack, level="beginner", interests=["writing"])
    cards = Cards(stack)
    try:
        when = datetime.now(UTC) - timedelta(hours=3)
        for n in range(14):
            await card(
                stack, title=f"Same {n:02d}", quality=5, category="tool", fit={"accountant": 100}
            )
        async with stack.owner.begin() as connection:
            await connection.execute(
                text("UPDATE items SET published_at = :w WHERE title LIKE 'Same %'"), {"w": when}
            )
        for n in range(6):
            await card(stack, title=f"Lower {n}", quality=2, category="news", hours=40 + n)
        await cards.clear()
        cards.live()
        expected = await pages(who, per_page=4)
        cards.cached(size)
        assert await pages(who, per_page=4) == expected
        seen = [t for page in expected for t in titles(page)]
        assert len(seen) == len(set(seen)) == 20  # nothing twice, nothing lost
    finally:
        await cards.close()


async def card_with_id(
    stack: Stack, ident: uuid.UUID, *, title: str, category: str, relevance: int
) -> None:
    """An article with an id of our choosing and everything else equal, so that scores can be made
    to meet exactly."""
    made = await card(stack, title=title, quality=3, category=category)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE items SET id = :new, published_at = :w WHERE id = :old"),
            {"new": ident, "old": made, "w": datetime.now(UTC) - timedelta(hours=5)},
        )
        await connection.execute(
            text(
                "INSERT INTO item_professions (org_id, item_id, profession_code, relevance) "
                "VALUES (:o, :i, 'accountant', :r)"
            ),
            {"o": stack.org_a, "i": ident, "r": relevance},
        )


@pytest.mark.anyio
async def test_equal_scores_that_come_from_different_parts_are_not_trusted_at_the_edge(
    make_stack: MakeStack,
) -> None:
    """The edge of the certain part is a strict one. Article X has a shared part 0.10 higher than
    the articles M but only the level bonus (0.05), the Ms have both (0.15): all score the same, and
    the live order among equals is by id. X has the lowest id, so it must come after every M; but the
    list is cut after X and five Ms, so it holds Ms the cut-off line would let it put before the rest.
    Trusting a score equal to the line would put X ahead of the Ms that are not in the list."""
    stack, who = await stack_and_person(make_stack, level="beginner", interests=["writing"])
    cards = Cards(stack)
    try:
        low = uuid.UUID(int=1)
        await card_with_id(stack, low, title="X", category="news", relevance=90)
        for n in range(10):
            await card_with_id(
                stack, uuid.UUID(int=0x10 + n), title=f"M{n}", category="tool", relevance=70
            )
        await cards.clear()
        cards.live()
        expected = await pages(who, per_page=4)
        order = [t for page in expected for t in titles(page)]
        assert order == [f"M{n}" for n in range(9, -1, -1)] + ["X"]  # the control: ties by id
        cards.cached(6)
        assert await pages(who, per_page=4) == expected
    finally:
        await cards.close()


def moment_of(page: dict[str, Any]) -> datetime:
    raw = page["next_cursor"]
    return datetime.fromisoformat(
        json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))["a"]
    )


@pytest.mark.anyio
async def test_while_a_ranking_is_being_made_a_first_page_is_ranked_at_the_previous_moment(
    make_stack: MakeStack,
) -> None:
    """F-50: the slowest answers of the load test were first pages that waited for their
    profession's new ranking. While another request makes it, the previous period's ranking
    answers at once, and every page of that visit is the live feed at the previous moment."""
    stack, who = await stack_and_person(make_stack, level="beginner", interests=["coding"])
    cards = Cards(stack)
    try:
        await fill(stack, 60)
        await cards.clear()
        cache = cards.cached(size=25)  # cut short: the live query takes over past it
        period = rank_moment_of(cards)
        previous = period - timedelta(minutes=5)
        cards.clock.value = previous + timedelta(seconds=150)
        assert moment_of(await feed(who, limit=5)) == previous  # the previous ranking is made

        cards.clock.value = period + timedelta(seconds=150)
        # Somebody else is making this period's ranking and has not finished.
        await cards.redis.set(
            f"{KEY_PREFIX}:{stack.org_a}:accountant:{int(period.timestamp())}:lock", "x"
        )
        cache._local.clear()  # read from Redis, as another API process would
        got = await pages(who, per_page=7)
        assert moment_of(got[0]) == previous
        assert len(await cards.keys()) == 2  # the previous ranking and the lock; nothing new made
        assert got == await live_pages_at(cards, who, previous, per_page=7)

        # The lock is gone: the next visit makes this period's ranking, in the background (F-61),
        # and is itself answered from the previous one; the visit after it is ranked at this period.
        await cards.redis.delete(
            f"{KEY_PREFIX}:{stack.org_a}:accountant:{int(period.timestamp())}:lock"
        )
        assert moment_of(await feed(who, limit=5)) == previous
        await cards.service.wait_for_rankings()
        assert moment_of(await feed(who, limit=5)) == period
    finally:
        await cards.close()


@pytest.mark.anyio
async def test_without_a_previous_ranking_a_first_page_still_waits_and_then_goes_live(
    make_stack: MakeStack,
) -> None:
    """The control of the test above: no previous ranking kept, so the request is answered at its
    own moment by the live query once the wait is over, as before F-50."""
    stack, who = await stack_and_person(make_stack)
    cards = Cards(stack)
    try:
        await fill(stack, 20)
        await cards.clear()
        cards.service._cache = RankingCache(cards.redis, wait_seconds=0.2)
        period = rank_moment_of(cards)
        await cards.redis.set(
            f"{KEY_PREFIX}:{stack.org_a}:accountant:{int(period.timestamp())}:lock", "x"
        )
        page = await feed(who, limit=5)
        assert moment_of(page) == period
        assert await cards.keys() == [
            f"{KEY_PREFIX}:{stack.org_a}:accountant:{int(period.timestamp())}:lock"
        ]
    finally:
        await cards.close()


def rank_moment_of(cards: Cards) -> datetime:
    value = cards.clock.value
    return value - timedelta(seconds=150)


async def live_pages_at(
    cards: Cards, who: Person, moment: datetime, per_page: int
) -> list[dict[str, Any]]:
    """The live feed's pages ranked at `moment`: the clock is moved into that period."""
    cached, value = cards.service._cache, cards.clock.value
    cards.live()
    cards.clock.value = moment + timedelta(seconds=150)
    try:
        return await pages(who, per_page=per_page)
    finally:
        cards.service._cache, cards.clock.value = cached, value


@pytest.mark.anyio
async def test_the_ranking_made_in_the_background_is_the_live_feed_of_its_period(
    make_stack: MakeStack,
) -> None:
    """F-61: the request that would make a new period's ranking is answered from the previous one
    and the ranking is made after it, in a transaction of its own; what it made pages exactly like
    the live query at the new moment."""
    stack, who = await stack_and_person(make_stack, level="advanced", interests=["research"])
    cards = Cards(stack)
    try:
        await fill(stack, 60)
        await cards.clear()
        cards.cached(size=25)
        period = rank_moment_of(cards)
        previous = period - timedelta(minutes=5)
        cards.clock.value = previous + timedelta(seconds=150)
        assert moment_of(await feed(who, limit=5)) == previous  # made at once: nothing to stand in
        cards.clock.value = period + timedelta(seconds=150)
        first = await feed(who, limit=5)
        assert moment_of(first) == previous  # answered at once from the previous ranking
        await cards.service.wait_for_rankings()
        made = f"{KEY_PREFIX}:{stack.org_a}:accountant:{int(period.timestamp())}"
        assert made in await cards.keys() and f"{made}:lock" not in await cards.keys()
        got = await pages(who, per_page=7)
        assert moment_of(got[0]) == period
        assert got == await live_pages_at(cards, who, period, per_page=7)
    finally:
        await cards.close()
