"""The feeds repository's own filters, with row-level security out of the way (M3-13).

Row-level security already hides another person's feeds, which is why the API tests cannot tell
whether the statements filter them too. These tests run the repository as the table owner, which
row-level security does not apply to, so only the statements themselves stand between one person
and another's feeds.
"""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from pharos.feeds import repository as feeds

from .test_feeds_api import make_item, post
from .test_local_auth import (  # noqa: F401  (fixtures and helpers used by these tests)
    MakeStack,
    Stack,
    make_stack,
)
from .test_sources_api import _remove_what_the_test_made  # noqa: F401  (the cleaning fixture)
from .test_user_admin import Person, person

pytestmark = pytest.mark.integration


@pytest.fixture
async def as_owner(make_stack: MakeStack) -> AsyncIterator[tuple[Stack, AsyncSession]]:
    stack: Stack = await make_stack("on")
    async with AsyncSession(stack.owner) as session:
        yield stack, session


async def feed_of(who: Person, n: int) -> uuid.UUID:
    response = await post(
        who, {"name": f"Feed {n}", "url": f"https://host{n}.example.org/feed.xml"}
    )
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])


@pytest.mark.anyio
async def test_every_statement_names_the_person(
    as_owner: tuple[Stack, AsyncSession], make_stack: MakeStack
) -> None:
    stack, session = as_owner
    alice = await person(stack, "member")
    bob = await person(stack, "member")
    mine = await feed_of(alice, 1)
    theirs = await feed_of(bob, 2)
    # positive control: the table owner sees both
    assert (await session.execute(text("SELECT count(*) FROM sources"))).scalar_one() == 2

    listed, total = await feeds.list_feeds(session, stack.org_a, alice.id, limit=50, offset=0)
    assert [f.id for f in listed] == [mine] and total == 1
    assert await feeds.count_feeds(session, stack.org_a, alice.id) == 1
    assert await feeds.get_feed(session, stack.org_a, alice.id, mine) is not None
    assert await feeds.get_feed(session, stack.org_a, alice.id, theirs) is None
    assert await feeds.get_feed(session, stack.org_a, alice.id, theirs, lock=True) is None
    assert await feeds.key_taken(session, stack.org_a, alice.id, "x" * 64) is False

    with pytest.raises(NoResultFound):
        await feeds.update_feed(
            session,
            stack.org_a,
            alice.id,
            theirs,
            name="Taken over",
            config={"url": "https://evil.example.org/"},
            dedupe_key="k",
            enabled=False,
            interval_seconds=3600,
        )
    await session.rollback()
    assert await feeds.delete_feed(session, stack.org_a, alice.id, theirs) is False
    await session.rollback()
    [row] = (
        await session.execute(text("SELECT name FROM sources WHERE id = :i"), {"i": theirs})
    ).all()
    assert row.name == "Feed 2"


@pytest.mark.anyio
async def test_the_articles_of_a_feed_are_those_of_the_person_and_the_feed(
    as_owner: tuple[Stack, AsyncSession],
) -> None:
    stack, session = as_owner
    alice = await person(stack, "member")
    bob = await person(stack, "member")
    mine = await feed_of(alice, 1)
    other = await feed_of(alice, 2)
    theirs = await feed_of(bob, 3)
    item = await make_item(stack, owner=alice.id, feeds=[mine], title="Mine")
    await make_item(stack, owner=alice.id, feeds=[other], title="Another feed of mine")
    await make_item(stack, owner=bob.id, feeds=[theirs], title="Theirs")
    # an organization article is never a personal one, even if someone linked it to the feed
    await make_item(stack, owner=None, feeds=[], title="Shared")

    found, total = await feeds.list_items(session, stack.org_a, alice.id, mine, limit=50, offset=0)
    assert [row.id for row in found] == [item] and total == 1
    # asking for the articles of somebody else's feed, as the owner of the table, finds nothing
    found, total = await feeds.list_items(
        session, stack.org_a, alice.id, theirs, limit=50, offset=0
    )
    assert found == [] and total == 0


@pytest.mark.anyio
async def test_the_organization_is_filtered_by_the_statements_too(
    as_owner: tuple[Stack, AsyncSession],
) -> None:
    stack, session = as_owner
    alice = await person(stack, "member")
    mine = await feed_of(alice, 1)
    assert await feeds.get_feed(session, stack.org_b, alice.id, mine) is None
    assert await feeds.count_feeds(session, stack.org_b, alice.id) == 0
    assert await feeds.recent_runs(session, stack.org_b, alice.id, mine, limit=5) == []
    listed, total = await feeds.list_feeds(session, stack.org_b, alice.id, limit=5, offset=0)
    assert listed == [] and total == 0


@pytest.mark.anyio
async def test_the_runs_of_a_feed_are_those_of_a_feed_of_the_person(
    as_owner: tuple[Stack, AsyncSession],
) -> None:
    stack, session = as_owner
    alice = await person(stack, "member")
    bob = await person(stack, "member")
    mine = await feed_of(alice, 1)
    theirs = await feed_of(bob, 2)
    for feed in (mine, theirs):
        await session.execute(
            text("INSERT INTO fetch_runs (org_id, source_id, outcome) VALUES (:o, :s, 'ok')"),
            {"o": stack.org_a, "s": feed},
        )
    await session.commit()
    assert len(await feeds.recent_runs(session, stack.org_a, alice.id, mine, limit=5)) == 1
    assert await feeds.recent_runs(session, stack.org_a, alice.id, theirs, limit=5) == []
