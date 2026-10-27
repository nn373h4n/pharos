import asyncio
import uuid

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.db import create_session_factory
from pharos.core.rls import content_policy_sql, tenant_policy_sql, tenant_session

from .conftest import APP_ROLE, Orgs, TestDatabase

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_application_role_is_unprivileged(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        role = (
            await connection.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :role"),
                {"role": APP_ROLE},
            )
        ).one()
    assert tuple(role) == (False, False)


@pytest.mark.anyio
async def test_every_table_has_rls_and_is_not_owned_by_the_application(
    owner_engine: AsyncEngine,
) -> None:
    async with owner_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT tablename, tableowner, c.relrowsecurity FROM pg_tables t "
                    "JOIN pg_class c ON c.relname = t.tablename "
                    "AND c.relnamespace = 'public'::regnamespace "
                    "WHERE t.schemaname = 'public' AND t.tablename <> 'alembic_version'"
                )
            )
        ).all()
    assert rows, "expected at least the organizations table"
    for name, owner, rls_enabled in rows:
        assert rls_enabled, f"{name} has no row-level security"
        assert owner != APP_ROLE, f"{name} is owned by the application role"


@pytest.mark.anyio
async def test_each_organization_sees_only_itself(app_engine: AsyncEngine, orgs: Orgs) -> None:
    factory = create_session_factory(app_engine)
    for org in orgs:
        async with tenant_session(factory, org) as session:
            visible = (await session.execute(text("SELECT id FROM organizations"))).scalars().all()
            assert visible == [org]


@pytest.mark.anyio
async def test_no_organization_means_no_rows_and_no_error(
    app_engine: AsyncEngine, orgs: Orgs
) -> None:
    factory = create_session_factory(app_engine)
    async with factory() as session:
        assert (await session.execute(text("SELECT id FROM organizations"))).all() == []
    # The same pooled connection after a scoped transaction: the setting is now an empty string.
    async with tenant_session(factory, orgs[0]):
        pass
    async with factory() as session:
        assert (await session.execute(text("SELECT id FROM organizations"))).all() == []


@pytest.mark.anyio
async def test_a_session_level_organization_does_not_leak_to_the_next_user_of_the_connection(
    app_engine: AsyncEngine, orgs: Orgs
) -> None:
    # Code (or an injected statement) can set app.org_id for the whole session, not just the
    # transaction. The pool must forget it when the connection is returned. The transaction has
    # to commit: PostgreSQL rolls a session-level SET back together with an aborted transaction.
    async with app_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :org, false)"), {"org": str(orgs[0])}
        )
        assert len((await connection.execute(text("SELECT id FROM organizations"))).all()) == 1

    factory = create_session_factory(app_engine)
    async with factory() as session:
        assert (await session.execute(text("SELECT id FROM organizations"))).all() == []


@pytest.mark.anyio
async def test_application_can_update_only_its_own_organization(
    app_engine: AsyncEngine, orgs: Orgs
) -> None:
    factory = create_session_factory(app_engine)
    alpha, beta = orgs
    async with tenant_session(factory, alpha) as session:
        own = await session.execute(
            text("UPDATE organizations SET name = 'Renamed' WHERE id = :id"), {"id": alpha}
        )
        other = await session.execute(
            text("UPDATE organizations SET name = 'Hijacked' WHERE id = :id"), {"id": beta}
        )
        assert (own.rowcount, other.rowcount) == (1, 0)  # type: ignore[attr-defined]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "assignment",
    ["id = gen_random_uuid()", "slug = 'taken-over'", "created_at = now()"],
)
async def test_application_cannot_change_immutable_organization_columns(
    app_engine: AsyncEngine, orgs: Orgs, assignment: str
) -> None:
    factory = create_session_factory(app_engine)
    statement = f"UPDATE organizations SET {assignment} WHERE id = :id"
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(factory, orgs[0]) as session:
            await session.execute(text(statement), {"id": orgs[0]})


@pytest.mark.anyio
async def test_application_cannot_create_or_delete_organizations(
    app_engine: AsyncEngine, orgs: Orgs
) -> None:
    factory = create_session_factory(app_engine)
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(factory, orgs[0]) as session:
            await session.execute(
                text("INSERT INTO organizations (name, slug) VALUES ('x', :slug)"),
                {"slug": uuid.uuid4().hex},
            )
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(factory, orgs[0]) as session:
            await session.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": orgs[0]})


@pytest.mark.anyio
async def test_tenant_policy_helper_isolates_a_table(
    owner_engine: AsyncEngine, app_engine: AsyncEngine, orgs: Orgs
) -> None:
    alpha, beta = orgs
    async with owner_engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE notes_rls_test "
                "(id serial PRIMARY KEY, org_id uuid NOT NULL, body text)"
            )
        )
        for statement in tenant_policy_sql("notes_rls_test"):
            await connection.execute(text(statement))
        await connection.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON notes_rls_test TO {APP_ROLE}")
        )
        await connection.execute(
            text(f"GRANT USAGE ON SEQUENCE notes_rls_test_id_seq TO {APP_ROLE}")
        )
    try:
        factory = create_session_factory(app_engine)
        insert = text("INSERT INTO notes_rls_test (org_id, body) VALUES (:org, :body)")
        async with tenant_session(factory, alpha) as session:
            await session.execute(insert, {"org": alpha, "body": "alpha note"})
        async with tenant_session(factory, beta) as session:
            await session.execute(insert, {"org": beta, "body": "beta note"})
            visible = (
                (await session.execute(text("SELECT body FROM notes_rls_test"))).scalars().all()
            )
            assert visible == ["beta note"]
            # Other tenants' rows can be neither changed nor deleted.
            changed = await session.execute(text("UPDATE notes_rls_test SET body = 'x'"))
            assert changed.rowcount == 1  # type: ignore[attr-defined]
        async with tenant_session(factory, alpha) as session:
            deleted = await session.execute(text("DELETE FROM notes_rls_test"))
            assert deleted.rowcount == 1  # type: ignore[attr-defined]
        with pytest.raises(DBAPIError, match="row-level security"):
            async with tenant_session(factory, alpha) as session:
                await session.execute(insert, {"org": beta, "body": "planted"})
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(text("DROP TABLE notes_rls_test"))


@pytest.mark.anyio
async def test_content_policy_shares_public_rows_read_only_and_isolates_org_rows(
    owner_engine: AsyncEngine, app_engine: AsyncEngine, orgs: Orgs
) -> None:
    alpha, beta = orgs
    async with owner_engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE items_rls_test (id serial PRIMARY KEY, scope text NOT NULL, "
                "owner_org_id uuid, title text)"
            )
        )
        for statement in content_policy_sql("items_rls_test"):
            await connection.execute(text(statement))
        await connection.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON items_rls_test TO {APP_ROLE}")
        )
        await connection.execute(
            text(f"GRANT USAGE ON SEQUENCE items_rls_test_id_seq TO {APP_ROLE}")
        )
        # Public content is written by the owner (in production: the ingestion role).
        await connection.execute(
            text("INSERT INTO items_rls_test (scope, title) VALUES ('public', 'public item')")
        )
    try:
        factory = create_session_factory(app_engine)
        insert = text("INSERT INTO items_rls_test (scope, owner_org_id, title) VALUES (:s, :o, :t)")
        async with tenant_session(factory, alpha) as session:
            await session.execute(insert, {"s": "org", "o": alpha, "t": "alpha only"})

        select = text("SELECT title FROM items_rls_test ORDER BY title")
        async with tenant_session(factory, alpha) as session:
            assert (await session.execute(select)).scalars().all() == ["alpha only", "public item"]
        async with tenant_session(factory, beta) as session:
            assert (await session.execute(select)).scalars().all() == ["public item"]

        # A tenant cannot touch the public rows every tenant shares: not rewrite, not hide, not
        # take over, not delete.
        async with tenant_session(factory, beta) as session:
            rewritten = await session.execute(
                text("UPDATE items_rls_test SET title = 'defaced' WHERE scope = 'public'")
            )
            taken = await session.execute(
                text(
                    "UPDATE items_rls_test SET scope = 'org', owner_org_id = :beta "
                    "WHERE scope = 'public'"
                ),
                {"beta": beta},
            )
            deleted = await session.execute(
                text("DELETE FROM items_rls_test WHERE scope = 'public'")
            )
            assert (rewritten.rowcount, taken.rowcount, deleted.rowcount) == (0, 0, 0)  # type: ignore[attr-defined]
        async with tenant_session(factory, alpha) as session:
            assert (await session.execute(select)).scalars().all() == ["alpha only", "public item"]

        # Creating public rows is not possible either, and neither is planting a row for another
        # organization.
        for scope, owner in (("public", None), ("public", alpha), ("org", beta)):
            with pytest.raises(DBAPIError, match=r"row-level security|scope_owner_check"):
                async with tenant_session(factory, alpha) as session:
                    await session.execute(insert, {"s": scope, "o": owner, "t": "planted"})

        # Own rows can be changed and deleted, but not turned into public ones.
        async with tenant_session(factory, alpha) as session:
            renamed = await session.execute(
                text("UPDATE items_rls_test SET title = 'renamed' WHERE owner_org_id = :o"),
                {"o": alpha},
            )
            assert renamed.rowcount == 1  # type: ignore[attr-defined]
        with pytest.raises(DBAPIError, match=r"row-level security|scope_owner_check"):
            async with tenant_session(factory, alpha) as session:
                await session.execute(
                    text(
                        "UPDATE items_rls_test SET scope = 'public', owner_org_id = NULL "
                        "WHERE owner_org_id = :o"
                    ),
                    {"o": alpha},
                )
        async with tenant_session(factory, alpha) as session:
            removed = await session.execute(
                text("DELETE FROM items_rls_test WHERE owner_org_id = :o"), {"o": alpha}
            )
            assert removed.rowcount == 1  # type: ignore[attr-defined]
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(text("DROP TABLE items_rls_test"))


@pytest.mark.anyio
async def test_baseline_migration_is_reversible(
    database: TestDatabase, owner_engine: AsyncEngine
) -> None:
    async def tables() -> set[str]:
        async with owner_engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {row[0] for row in rows}

    assert "organizations" in await tables()
    # alembic's command API is synchronous and starts its own event loop, so run it in a thread.
    try:
        await asyncio.to_thread(command.downgrade, database.alembic, "base")
        assert "organizations" not in await tables()
    finally:
        await asyncio.to_thread(command.upgrade, database.alembic, "head")
    assert "organizations" in await tables()
