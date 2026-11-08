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
        return [
            key.decode() if isinstance(key, bytes) else key
            async for key in self.redis.scan_iter(match=f"{KEY_PREFIX}:*")
        ]

    async def close(self) -> None:
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
        cards.clock.value += timedelta(seconds=300)  # the next period is ranked again
        await feed(first)
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
