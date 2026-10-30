"""The items repository's own filters, with row-level security out of the way (M3-10b).

Row-level security already hides another organization's items and a person's own articles, which is
why the API tests cannot tell whether the statements filter them too. These tests remove that second
line in two ways: they name the owner to the database (as M3-13 will), and they run as the table
owner, which row-level security does not apply to.
"""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import pytest
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import async_sessionmaker

from pharos.core.rls import tenant_session
from pharos.items import repository as items
from pharos.sources import repository as sources

from .test_items_api import make_item, status_of
from .test_local_auth import (  # noqa: F401  (fixtures and helpers used by these tests)
    MakeStack,
    Stack,
    make_stack,
)
from .test_sources_api import (  # noqa: F401  (the cleaning fixture applies to this module too)
    _remove_what_the_test_made,
    foreign_source,
)
from .test_user_admin import person

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_the_repository_never_touches_a_personal_article_even_for_its_owner(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    owner = await person(stack, "editor")
    personal = await make_item(stack, owner=owner.id, title="Private")
    shared = await make_item(stack, title="Shared")
    factory = stack.app.state.session_factory

    async with tenant_session(factory, stack.org_a) as session:
        await sources.act_as(session, owner.id)
        # positive control: with the owner named, the database itself shows both articles
        assert (await session.execute(text("SELECT count(*) FROM items"))).scalar_one() == 2
        listed, total = await items.list_items(
            session, stack.org_a, status=None, source=None, limit=50, offset=0
        )
        assert [i.id for i in listed] == [shared] and total == 1
        assert await items.get_item(session, stack.org_a, personal) is None
        assert await items.get_item(session, stack.org_a, personal, lock=True) is None
        assert await items.get_item(session, stack.org_a, shared) is not None

    async with tenant_session(factory, stack.org_a) as session:
        await sources.act_as(session, owner.id)
        with pytest.raises(NoResultFound):
            await items.set_status(
                session, stack.org_a, personal, status="hidden", hold_reason=None
            )
        await session.rollback()
    assert await status_of(stack, personal) == ("new", None)


@pytest.mark.anyio
async def test_the_repository_filters_by_organization_itself_not_only_through_row_security(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    ours = await make_item(stack)
    their_source = await foreign_source(stack)
    theirs = await make_item(
        stack, org=stack.org_b, status="held", hold_reason="link_ip_host", sources=(their_source,)
    )
    sessions = async_sessionmaker(stack.owner, expire_on_commit=False)
    async with sessions() as session:
        # positive controls: this connection sees both organizations, and the link exists
        assert (await session.execute(text("SELECT count(*) FROM items"))).scalar_one() == 2
        assert (await session.execute(text("SELECT count(*) FROM item_sources"))).scalar_one() == 1
        listed, total = await items.list_items(
            session, stack.org_a, status=None, source=None, limit=50, offset=0
        )
        assert [i.id for i in listed] == [ours] and total == 1
        held, held_total = await items.list_items(
            session, stack.org_a, status="held", source=None, limit=50, offset=0
        )
        assert held == [] and held_total == 0
        filtered, filtered_total = await items.list_items(
            session, stack.org_a, status=None, source=their_source, limit=50, offset=0
        )
        assert filtered == [] and filtered_total == 0
        assert await items.get_item(session, stack.org_a, theirs) is None
        assert await items.sources_of(session, stack.org_a, [theirs]) == {}
        with pytest.raises(NoResultFound):
            await items.set_status(session, stack.org_a, theirs, status="hidden", hold_reason=None)
        await session.rollback()
    assert await status_of(stack, theirs) == ("held", "link_ip_host")
