"""The repository's own filters, with the database's row-level rules out of the way (M3-10a).

Row-level security hides a person's own feeds from everybody else, and that is why the API tests
cannot tell whether the repository filters them too. These tests name the owner to the database
(`app.user_id`, as M3-13 will), so only the statements themselves keep the feed out of reach.
"""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import async_sessionmaker

from pharos.core.rls import tenant_session
from pharos.sources import repository

from .test_local_auth import (  # noqa: F401  (fixtures and helpers used by these tests)
    MakeStack,
    Stack,
    make_stack,
)
from .test_sources_api import (  # noqa: F401  (the cleaning fixture applies to this module too)
    FEED,
    _remove_what_the_test_made,
    foreign_source,
    post,
    rows,
)
from .test_user_admin import person

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_the_repository_never_touches_a_personal_feed_even_for_its_owner(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    owner = await person(stack, "admin")
    personal = await foreign_source(stack, org_id=stack.org_a, owner_user_id=owner.id)
    shared = uuid.UUID((await post(owner, FEED)).json()["id"])
    factory = stack.app.state.session_factory

    async with tenant_session(factory, stack.org_a) as session:
        await repository.act_as(session, owner.id)
        # positive control: with the owner named, the database itself would show both rows
        assert (await session.execute(text("SELECT count(*) FROM sources"))).scalar_one() == 2
        listed, total = await repository.list_sources(
            session, stack.org_a, limit=50, offset=0, source_type=None, enabled=None
        )
        assert [s.id for s in listed] == [shared] and total == 1
        assert await repository.count_sources(session, stack.org_a) == 1
        assert await repository.get_source(session, stack.org_a, personal) is None
        assert await repository.get_source(session, stack.org_a, personal, lock=True) is None
        assert await repository.get_source(session, stack.org_a, shared) is not None
        assert not await repository.delete_source(session, stack.org_a, personal)
        key = (
            await session.execute(
                text("SELECT dedupe_key FROM sources WHERE id = :i"), {"i": personal}
            )
        ).scalar_one()
        assert not await repository.key_taken(session, stack.org_a, key)  # not the same namespace

    async with tenant_session(factory, stack.org_a) as session:
        await repository.act_as(session, owner.id)
        with pytest.raises(NoResultFound):
            await repository.update_source(
                session,
                stack.org_a,
                personal,
                name="Taken over",
                config={},
                dedupe_key="x",
                enabled=True,
                interval_seconds=3600,
            )
        await session.rollback()

    async with stack.owner.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT name, enabled FROM sources WHERE id = :i"), {"i": personal}
            )
        ).one()
    assert (row.name, row.enabled) == ("Not yours", False)


@pytest.mark.anyio
async def test_the_repository_filters_by_organization_itself_not_only_through_row_security(
    make_stack: MakeStack,
) -> None:
    """The table owner is not subject to row-level security, so only the statements' own
    `org_id` filters can keep the other organization's source out of reach."""
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    ours = uuid.UUID((await post(admin, FEED)).json()["id"])
    theirs = await foreign_source(stack)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("INSERT INTO fetch_runs (org_id, source_id, outcome) VALUES (:o, :s, 'ok')"),
            {"o": stack.org_b, "s": theirs},
        )
    sessions = async_sessionmaker(stack.owner, expire_on_commit=False)
    async with sessions() as session:
        # positive control: this connection sees both organizations
        assert (await session.execute(text("SELECT count(*) FROM sources"))).scalar_one() == 2
        listed, total = await repository.list_sources(
            session, stack.org_a, limit=50, offset=0, source_type=None, enabled=None
        )
        assert [s.id for s in listed] == [ours] and total == 1
        assert await repository.count_sources(session, stack.org_a) == 1
        assert await repository.get_source(session, stack.org_a, theirs) is None
        assert await repository.get_source(session, stack.org_a, ours) is not None
        assert (await session.execute(text("SELECT count(*) FROM fetch_runs"))).scalar_one() == 1
        assert await repository.recent_runs(session, stack.org_a, theirs, limit=5) == []
        assert not await repository.delete_source(session, stack.org_a, theirs)
        with pytest.raises(NoResultFound):
            await repository.update_source(
                session,
                stack.org_a,
                theirs,
                name="x",
                config={},
                dedupe_key="x",
                enabled=True,
                interval_seconds=3600,
            )
        await session.rollback()
    remaining = await rows(stack, "SELECT 1 FROM sources WHERE id = :i", i=theirs)
    assert len(remaining) == 1
