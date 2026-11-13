"""One card for one story (M4-05, ADR-0021): of the articles that share a cluster, the feed shows
the one seen first, and the others stay in the database. Quality, which a model sets from text a
source wrote, only breaks a tie."""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from pharos.cards.ranking_cache import KEY_PREFIX

from .test_cards_api import card, feed, put, stack_and_person, titles
from .test_local_auth import MakeStack, Stack, make_stack  # noqa: F401
from .test_sources_api import _remove_what_the_test_made  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def a_minute_later(stack: Stack) -> None:
    """The feed is ranked once in five minutes for everybody (ADR-0027): a change to which copy stands for
    a story, or to whether it may be shown, reaches the ranking at the next minute. A test that
    changes the articles and looks again lets that minute pass by forgetting the kept ranking."""
    redis = Redis.from_url(stack.settings.redis_url.get_secret_value())
    async for key in redis.scan_iter(match=f"{KEY_PREFIX}:*"):
        await redis.delete(key)
    await redis.aclose()
    stack.app.state.cards._cache._local.clear()  # and the copies this API process keeps (F-50)


async def cluster(stack: Stack, *items: uuid.UUID, id: uuid.UUID | None = None) -> uuid.UUID:
    found = id or uuid.uuid4()
    async with stack.owner.begin() as connection:
        for item in items:
            await connection.execute(
                text("UPDATE items SET cluster_id = :c WHERE id = :i"), {"c": found, "i": item}
            )
    return found


async def three(stack: Stack, **quality: int) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Three copies of one story, created in this order: the first seen, a better one, the last."""
    first = await card(stack, title="first", quality=quality.get("first", 3), hours=3)
    best = await card(stack, title="best", quality=quality.get("best", 5), hours=2)
    last = await card(stack, title="last", quality=quality.get("last", 3), hours=1)
    await cluster(stack, first, best, last)
    return first, best, last


async def test_a_story_is_one_card_and_it_is_the_copy_seen_first(make_stack: MakeStack) -> None:
    stack, who = await stack_and_person(make_stack)
    await three(stack)
    await card(stack, title="alone")
    assert sorted(titles(await feed(who))) == ["alone", "first"]


async def test_a_later_copy_that_scores_higher_never_pushes_the_original_out(
    make_stack: MakeStack,
) -> None:
    """Review of M4-05 (P2): quality is model output over text a source wrote, so a syndicated copy
    that talks the model into a 5 must not replace the original's link for every reader."""
    stack, who = await stack_and_person(make_stack)
    original = await card(stack, title="original", quality=1)
    copy = await card(stack, title="copy", quality=5)
    await cluster(stack, original, copy)
    page = await feed(who)
    assert titles(page) == ["original"] and page["items"][0]["id"] == str(original)


async def test_quality_breaks_a_tie_between_copies_seen_at_the_same_moment(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    low = await card(stack, title="low", quality=2)
    high = await card(stack, title="high", quality=4)
    await cluster(stack, low, high)
    assert titles(await feed(who)) == ["low"]  # control: seen first still wins
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "UPDATE items SET created_at = (SELECT created_at FROM items WHERE id = :a) "
                "WHERE id = :b"
            ),
            {"a": low, "b": high},
        )
    await a_minute_later(stack)
    assert titles(await feed(who)) == ["high"]


async def test_equally_good_copies_show_the_one_seen_first(make_stack: MakeStack) -> None:
    stack, who = await stack_and_person(make_stack)
    first, _, _ = await three(stack, best=3)
    page = await feed(who)
    assert sorted(titles(page)) == ["first"] and page["items"][0]["id"] == str(first)


async def test_when_the_best_copy_cannot_be_shown_the_next_one_takes_its_place(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    first, best, _last = await three(stack)
    assert titles(await feed(who)) == ["first"]  # control: the first one stands for the story
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE items SET status = 'held', hold_reason = 'editor_hold' WHERE id = :i"),
            {"i": first},
        )
    await a_minute_later(stack)
    assert titles(await feed(who)) == ["best"]  # the story did not disappear
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE items SET status = 'hidden' WHERE id = :i"), {"i": best}
        )
    await a_minute_later(stack)
    assert titles(await feed(who)) == ["last"]


async def test_a_copy_that_is_not_a_card_never_hides_another(make_stack: MakeStack) -> None:
    stack, who = await stack_and_person(make_stack)
    shown = await card(stack, title="shown", quality=2)
    better_but_not_a_card = await card(stack, title="unread-better", quality=5)
    personal = await card(stack, title="personal", quality=5, owner=who.id)
    async with stack.owner.begin() as connection:
        await connection.execute(  # read, but found irrelevant: no summary
            text("UPDATE items SET summary = NULL, category = NULL, quality = NULL WHERE id = :i"),
            {"i": better_but_not_a_card},
        )
    await cluster(stack, shown, better_but_not_a_card, personal)
    assert titles(await feed(who)) == ["shown"]


async def test_a_story_a_person_marked_not_useful_is_gone_with_its_copies(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    first, _, _ = await three(stack)
    await card(stack, title="alone")
    assert (await put(who, first, "not_useful")).status_code in (200, 204)
    assert titles(await feed(who)) == ["alone"]


async def test_another_organization_with_the_same_cluster_id_changes_nothing(
    make_stack: MakeStack,
) -> None:
    stack, who = await stack_and_person(make_stack)
    ours = await card(stack, title="ours", quality=1)
    theirs = await card(stack, title="theirs", quality=5, org=stack.org_b)
    shared = await cluster(stack, ours)
    await cluster(stack, theirs, id=shared)
    assert titles(await feed(who)) == ["ours"]


async def test_paging_gives_every_story_once(make_stack: MakeStack) -> None:
    stack, who = await stack_and_person(make_stack)
    for n in range(4):
        group = [
            await card(stack, title=f"story{n}-{c}", quality=1 + c, hours=n + 1 + c / 10)
            for c in range(3)
        ]
        await cluster(stack, *group)
    for n in range(3):
        await card(stack, title=f"single{n}", hours=n + 1)
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = await feed(who, **params)
        seen += titles(page)
        cursor = page.get("next_cursor")
        if not cursor:
            break
    stories = sorted(t.split("-")[0] for t in seen if t.startswith("story"))
    assert stories == ["story0", "story1", "story2", "story3"]  # each story once
    assert sorted(t for t in seen if t.startswith("single")) == ["single0", "single1", "single2"]
    assert len(seen) == len(set(seen)) == 7


# --- the acceptance: the duplicate rate on a corpus ---------------------------------------------------


async def test_a_corpus_of_syndicated_copies_shows_few_duplicates_and_loses_no_story(
    make_stack: MakeStack, database: Any
) -> None:
    """M4-05 acceptance: a duplicate rate under 5%. The corpus is SYNTHETIC: sixty stories of eight
    words of their own, forty of which have a syndicated copy (the words reordered, one filler
    word). The embedder is the demo one (hashed words), so this measures the path - sweep,
    clusters, feed - and not any language or model."""
    from pharos.core.db import create_engine
    from pharos.llm import clustering
    from pharos.llm.embedding import DemoEmbedder, Embedded, check_vector, embedding_text
    from pharos.llm.types import PublicArticleText

    stack, who = await stack_and_person(make_stack)
    stories, copies = 60, 40

    def words(n: int) -> list[str]:
        return [f"t{n}k{k}" for k in range(8)]

    made = 0
    for n in range(stories):
        w = words(n)
        await card(stack, title=" ".join(w), hours=1 + n / 100)
        made += 1
        if n < copies:
            shuffled = list(reversed(w))
            await card(
                stack,
                title="Report " + " ".join(shuffled).upper(),
                hours=0.5 + n / 100,
                quality=2,
            )
            made += 1
    assert made == stories + copies

    class Gateway:
        async def embed(self, *, org_id: uuid.UUID, article: PublicArticleText) -> Embedded:
            answer = await DemoEmbedder().embed(
                model="m", texts=[embedding_text(article)], deadline_seconds=1
            )
            return Embedded("m", check_vector(answer))

    engine = create_engine(database.worker_url)
    try:
        done = await clustering.sweep(
            engine, Gateway(), stack.org_a, model="m", batch=500, threshold=0.92, window_days=14
        )
    finally:
        await engine.dispose()
    # A hashed bag of words collides now and then, so one copy in forty can stay unjoined.
    assert done.embedded == made and done.failed == 0 and copies - 3 <= done.clustered <= copies

    shown: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, Any] = {"limit": 50}
        if cursor:
            params["cursor"] = cursor
        page = await feed(who, **params)
        shown += titles(page)
        cursor = page.get("next_cursor")
        if not cursor:
            break
    import re

    story_of = [int(re.search(r"t(\d+)k", t.lower()).group(1)) for t in shown]  # type: ignore[union-attr]
    duplicates = len(story_of) - len(set(story_of))
    rate = duplicates / len(story_of)
    print(f"DUPLICATES {duplicates} of {len(story_of)} cards ({rate * 100:.1f}%)")
    assert rate < 0.05
    assert set(story_of) == set(range(stories))  # and no story was lost behind another
    # Without the clusters the same feed would have shown every copy (the control for the number).
    assert stories <= len(story_of) < made
