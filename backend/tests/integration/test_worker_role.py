"""The worker's database role sees nothing about people or sign-in (ADR-0012, M3-03).

The role is created by deploy/postgres/init/01-roles.sh in a deployment and by conftest.py here, with
the same attributes. Migrations grant it access to the ingestion tables only, so until a table is on
the allow-list below it must have no privilege at all. Every "cannot" has a positive control: the
application role can do the same thing.
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pharos.core.db import create_engine
from pharos.workers.runtime import WorkerRoleError, WorkerRuntime, check_worker_role

from .conftest import APP_ROLE, WORKER_ROLE, Orgs, TestDatabase

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Tables the worker may touch, with the privileges it may hold. Grows with migration 0003 (M3-05);
# every entry needs a reason in the migration.
ALLOWED: dict[str, set[str]] = {
    "sources": {"SELECT"},
    "fetch_runs": {"SELECT", "DELETE"},
    "raw_items": {"SELECT", "DELETE"},
    "items": {"SELECT", "DELETE"},
    "item_sources": {"SELECT"},
}
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


@pytest.fixture
async def worker_engine(database: TestDatabase):  # type: ignore[no-untyped-def]
    engine = create_engine(database.worker_url)
    yield engine
    await engine.dispose()


async def scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as connection:
        return (await connection.execute(text(sql), params)).scalar_one()


async def test_the_worker_role_has_no_special_powers(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolcanlogin "
                    "FROM pg_roles WHERE rolname = :role"
                ),
                {"role": WORKER_ROLE},
            )
        ).one()
    assert tuple(row) == (False, False, False, False, True)


async def test_the_worker_has_no_privilege_on_any_table_but_the_allowed_ones(
    owner_engine: AsyncEngine,
) -> None:
    async with owner_engine.connect() as connection:
        tables = [
            row[0]
            for row in await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        ]
        assert "users" in tables and "local_credentials" in tables  # the query sees the tables
        held: dict[str, set[str]] = {}
        for table in tables:
            for privilege in PRIVILEGES:
                row = await connection.execute(
                    text("SELECT has_table_privilege(:role, :table, :privilege)"),
                    {"role": WORKER_ROLE, "table": f"public.{table}", "privilege": privilege},
                )
                if row.scalar_one():
                    held.setdefault(table, set()).add(privilege)
            columns = await connection.execute(
                text("SELECT has_any_column_privilege(:role, :table, 'SELECT,INSERT,UPDATE')"),
                {"role": WORKER_ROLE, "table": f"public.{table}"},
            )
            if columns.scalar_one() and table not in ALLOWED:
                held.setdefault(table, set()).add("COLUMN")
        # Positive control: the same query reports what the application role does hold.
        control = await connection.execute(
            text("SELECT has_table_privilege(:role, 'public.users', 'SELECT')"),
            {"role": APP_ROLE},
        )
        assert control.scalar_one() is True
    unexpected = {t: p for t, p in held.items() if t not in ALLOWED or not p <= ALLOWED[t]}
    assert unexpected == {}


@pytest.mark.parametrize(
    "table",
    ["users", "local_credentials", "audit_logs", "user_profiles", "departments", "organizations"],
)
async def test_the_worker_cannot_read_people_or_sign_in_tables(
    worker_engine: AsyncEngine, app_engine: AsyncEngine, table: str
) -> None:
    assert await scalar(app_engine, f"SELECT count(*) >= 0 FROM {table}") is True  # control
    with pytest.raises(DBAPIError, match="permission denied"):
        await scalar(worker_engine, f"SELECT count(*) FROM {table}")


async def test_the_worker_cannot_write_or_create(worker_engine: AsyncEngine) -> None:
    for statement in (
        "INSERT INTO organizations (name, slug) VALUES ('x', 'worker-made')",
        "UPDATE users SET role = 'admin'",
        "DELETE FROM audit_logs",
        "CREATE TABLE worker_probe (id int)",
        "CREATE SCHEMA worker_probe",
    ):
        async with worker_engine.connect() as connection:
            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(text(statement))


async def test_the_worker_cannot_become_another_role(worker_engine: AsyncEngine) -> None:
    for statement in (
        f"SET ROLE {APP_ROLE}",
        "SET ROLE pharos_owner",
        "SET SESSION AUTHORIZATION postgres",
    ):
        async with worker_engine.connect() as connection:
            with pytest.raises(DBAPIError, match=r"permission denied|must be"):
                await connection.execute(text(statement))


async def test_the_other_roles_passwords_do_not_open_the_worker_role(
    database: TestDatabase,
) -> None:
    wrong = database.worker_url.replace("worker-test-only", "app-test-only")
    engine = create_async_engine(wrong)
    try:
        with pytest.raises(DBAPIError, match="password authentication failed"):
            async with engine.connect():
                pass
    finally:
        await engine.dispose()


# --- the start-up check -------------------------------------------------------------------------


async def test_the_start_up_check_accepts_the_worker_role(worker_engine: AsyncEngine) -> None:
    await check_worker_role(worker_engine)


@pytest.mark.parametrize(
    ("setup", "teardown"),
    [
        ("ALTER ROLE pharos_worker CREATEDB", "ALTER ROLE pharos_worker NOCREATEDB"),
        ("ALTER ROLE pharos_worker CREATEROLE", "ALTER ROLE pharos_worker NOCREATEROLE"),
        ("ALTER ROLE pharos_worker BYPASSRLS", "ALTER ROLE pharos_worker NOBYPASSRLS"),
        ("ALTER ROLE pharos_worker SUPERUSER", "ALTER ROLE pharos_worker NOSUPERUSER"),
    ],
)
async def test_the_start_up_check_refuses_a_worker_role_that_has_become_powerful(
    worker_engine: AsyncEngine, setup: str, teardown: str
) -> None:
    """The right name is not enough: a role someone widened later must stop the worker."""
    admin = create_async_engine(os.environ["PHAROS_TEST_ADMIN_URL"], isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(setup))
        try:
            with pytest.raises(WorkerRoleError, match="special attributes"):
                await check_worker_role(worker_engine)
        finally:
            async with admin.connect() as connection:
                await connection.execute(text(teardown))
        await check_worker_role(worker_engine)  # positive control: back to normal, accepted
    finally:
        await admin.dispose()


async def test_the_start_up_check_refuses_a_worker_role_that_owns_a_table(
    database: TestDatabase, worker_engine: AsyncEngine
) -> None:
    admin = create_async_engine(
        make_url(os.environ["PHAROS_TEST_ADMIN_URL"]).set(database=database.name),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin.connect() as connection:
            await connection.execute(text("CREATE TABLE public.worker_owned_probe (id int)"))
            await connection.execute(
                text("ALTER TABLE public.worker_owned_probe OWNER TO pharos_worker")
            )
        try:
            with pytest.raises(WorkerRoleError, match="special attributes"):
                await check_worker_role(worker_engine)
        finally:
            async with admin.connect() as connection:
                await connection.execute(text("DROP TABLE public.worker_owned_probe"))
        await check_worker_role(worker_engine)
    finally:
        await admin.dispose()


@pytest.mark.parametrize(
    ("setup", "teardown"),
    [
        ("GRANT pharos_owner TO pharos_worker", "REVOKE pharos_owner FROM pharos_worker"),
        ("GRANT pharos_app TO pharos_worker", "REVOKE pharos_app FROM pharos_worker"),
        ("GRANT pg_read_all_data TO pharos_worker", "REVOKE pg_read_all_data FROM pharos_worker"),
        (
            "GRANT pg_read_server_files TO pharos_worker",
            "REVOKE pg_read_server_files FROM pharos_worker",
        ),
        (
            "CREATE SEQUENCE public.worker_owned_probe; "
            "ALTER SEQUENCE public.worker_owned_probe OWNER TO pharos_worker",
            "DROP SEQUENCE public.worker_owned_probe",
        ),
        (
            "CREATE VIEW public.worker_owned_probe AS SELECT 1 AS x; "
            "ALTER VIEW public.worker_owned_probe OWNER TO pharos_worker",
            "DROP VIEW public.worker_owned_probe",
        ),
        (
            "CREATE FUNCTION public.worker_owned_probe() RETURNS int LANGUAGE sql AS 'SELECT 1'; "
            "ALTER FUNCTION public.worker_owned_probe() OWNER TO pharos_worker",
            "DROP FUNCTION public.worker_owned_probe()",
        ),
        (
            "CREATE SCHEMA worker_owned_probe AUTHORIZATION pharos_worker",
            "DROP SCHEMA worker_owned_probe",
        ),
    ],
)
async def test_the_start_up_check_refuses_a_worker_role_that_joined_a_role_or_owns_something(
    database: TestDatabase, worker_engine: AsyncEngine, setup: str, teardown: str
) -> None:
    admin = create_async_engine(
        make_url(os.environ["PHAROS_TEST_ADMIN_URL"]).set(database=database.name),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin.connect() as connection:
            for statement in setup.split("; "):
                await connection.execute(text(statement))
        try:
            with pytest.raises(WorkerRoleError, match="special attributes"):
                await check_worker_role(worker_engine)
        finally:
            async with admin.connect() as connection:
                await connection.execute(text(teardown))
        await check_worker_role(worker_engine)  # positive control: back to normal, accepted
    finally:
        await admin.dispose()


async def test_the_worker_cannot_make_temporary_tables(worker_engine: AsyncEngine) -> None:
    # As in a deployment, the database is closed to PUBLIC (01-roles.sh), so TEMP is not inherited.
    async with worker_engine.connect() as connection:
        with pytest.raises(DBAPIError, match="permission denied"):
            await connection.execute(text("CREATE TEMP TABLE worker_probe (id int)"))


async def test_the_worker_finds_its_organization_through_the_lookup_and_nothing_else(
    worker_engine: AsyncEngine, app_engine: AsyncEngine, orgs: Orgs, owner_engine: AsyncEngine
) -> None:
    async with owner_engine.connect() as connection:
        slug = (
            await connection.execute(
                text("SELECT slug FROM organizations WHERE id = :id"), {"id": orgs[0]}
            )
        ).scalar_one()
    lookup = "SELECT pharos_org_id_for_slug(:slug)"
    for engine in (app_engine, worker_engine):  # the worker may, as the API may
        async with engine.connect() as connection:
            found = (await connection.execute(text(lookup), {"slug": slug})).scalar_one()
        assert found == orgs[0]
    # The lookup is all it gets: the table itself stays closed (see the table tests above).
    async with worker_engine.connect() as connection:
        with pytest.raises(DBAPIError, match="permission denied"):
            await connection.execute(text("SELECT id FROM organizations"))


async def test_the_start_up_check_refuses_the_application_role(app_engine: AsyncEngine) -> None:
    with pytest.raises(WorkerRoleError, match="not 'pharos_worker'"):
        await check_worker_role(app_engine)


async def test_the_start_up_check_refuses_the_owner_role(owner_engine: AsyncEngine) -> None:
    with pytest.raises(WorkerRoleError, match="not 'pharos_worker'"):
        await check_worker_role(owner_engine)


# --- the per-process runtime against a real database --------------------------------------------


def test_tasks_run_as_the_worker_role_on_one_loop_and_one_engine(database: TestDatabase) -> None:
    runtime = WorkerRuntime(lambda: database.worker_url)
    seen: list[tuple[int, int]] = []

    async def whoami(engine: AsyncEngine) -> str:
        import asyncio

        seen.append((id(asyncio.get_running_loop()), id(engine)))
        async with engine.connect() as connection:
            return str((await connection.execute(text("SELECT current_user"))).scalar_one())

    try:
        assert [runtime.run(whoami) for _ in range(3)] == [WORKER_ROLE] * 3
        assert len(set(seen)) == 1  # the same loop and the same engine every time
    finally:
        runtime.close()
