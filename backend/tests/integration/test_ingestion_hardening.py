"""Migration 0003, second part: what the security review of the schema asked to be proven.

Personal feeds are limited by the database itself, nobody can forge attribution or turn a personal
source into an organization one, the worker cannot undo a person's decision, the exact privileges of
both roles are pinned, and the personal-row policy holds for a role nobody thought of.
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .conftest import TestDatabase
from .ingestion_support import Org, add_item, add_source, count, session

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# --- personal feeds are limited by the database --------------------------------------------------


async def test_a_personal_feed_is_fetched_at_most_hourly(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        await add_source(connection, alpha, key="org-fast", interval_seconds=300)  # control
        await add_source(
            connection, alpha, key="mine-slow", owner=alpha.user, interval_seconds=3600
        )
    async with owner_engine.connect() as connection:
        with pytest.raises(IntegrityError, match="sources_personal_interval_check"):
            await add_source(
                connection, alpha, key="mine-fast", owner=alpha.user, interval_seconds=3599
            )


async def test_a_person_can_have_five_feeds_of_their_own_and_not_six(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        for n in range(5):
            await add_source(connection, alpha, key=f"mine-{n}", owner=alpha.user)
        await add_source(connection, alpha, key="theirs", owner=alpha.other_user)  # not counted
        for n in range(8):
            await add_source(connection, alpha, key=f"org-{n}")  # organization sources not counted
    async with owner_engine.connect() as connection:
        with pytest.raises(IntegrityError, match="at most five"):
            await add_source(connection, alpha, key="mine-6", owner=alpha.user)


async def test_parallel_requests_cannot_get_past_the_limit(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta

    async def attempt(n: int) -> bool:
        try:
            async with owner_engine.begin() as connection:
                await add_source(connection, alpha, key=f"race-{n}", owner=alpha.user)
                await asyncio.sleep(0.05)  # hold the transaction open so the others overlap
            return True
        except IntegrityError:
            return False

    results = await asyncio.gather(*(attempt(n) for n in range(10)))
    assert sum(results) == 5
    async with owner_engine.connect() as connection:
        total = (
            await connection.execute(
                text("SELECT count(*) FROM sources WHERE owner_user_id = :u"), {"u": alpha.user}
            )
        ).scalar_one()
    assert total == 5


# --- nobody can forge or reassign ----------------------------------------------------------------


async def test_attribution_cannot_be_forged(
    app_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with session(app_engine, alpha, alpha.user) as connection:
        await add_source(connection, alpha, key="mine", created_by=alpha.user)  # control
        await add_source(connection, alpha, key="nobody", created_by=None)
    async with session(app_engine, alpha, alpha.user) as connection:
        with pytest.raises(DBAPIError, match="row-level security"):
            await add_source(connection, alpha, key="forged", created_by=alpha.other_user)
    async with session(app_engine, alpha) as connection:  # no person named: only NULL is allowed
        with pytest.raises(DBAPIError, match="row-level security"):
            await add_source(connection, alpha, key="forged-2", created_by=alpha.user)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE sources SET owner_user_id = NULL",
        "UPDATE sources SET owner_user_id = gen_random_uuid()",
        "UPDATE sources SET created_by = gen_random_uuid()",
        "UPDATE sources SET next_fetch_at = now()",
        "UPDATE items SET owner_user_id = NULL",
        "UPDATE items SET owner_user_id = gen_random_uuid()",
    ],
)
async def test_a_source_cannot_change_hands_or_be_fetched_on_demand_by_writing_to_it(
    app_engine: AsyncEngine,
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    statement: str,
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        await add_source(connection, alpha, key="mine", owner=alpha.user)
        await add_item(connection, alpha, url="https://example.com/m", owner=alpha.user)
    for engine in (app_engine, worker_engine):
        for user in (None, alpha.user):
            if engine is worker_engine and statement.startswith("UPDATE sources SET next_fetch_at"):
                continue  # the worker is the one that schedules fetches
            async with session(engine, alpha, user) as connection:
                with pytest.raises(DBAPIError, match="permission denied"):
                    await connection.execute(text(statement))


async def test_the_worker_cannot_see_who_set_a_source_up(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        await add_source(connection, alpha, key="feed", created_by=alpha.user)
    async with session(worker_engine, alpha) as connection:
        assert await count(connection, "sources") == 1  # control: it reads everything else
        rows = (await connection.execute(text("SELECT id, name, config FROM sources"))).all()
        assert len(rows) == 1
    async with session(worker_engine, alpha) as connection:
        with pytest.raises(DBAPIError, match="permission denied"):
            await connection.execute(text("SELECT created_by FROM sources"))


# --- the worker cannot undo a person's decision -----------------------------------------------------


async def test_the_worker_cannot_delete_a_hidden_item_or_recreate_one_as_hidden(
    worker_engine: AsyncEngine,
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        hidden = await add_item(connection, alpha, url="https://example.com/hidden")
        fresh = await add_item(connection, alpha, url="https://example.com/fresh")
    async with session(app_engine, alpha) as connection:
        await connection.execute(
            text("UPDATE items SET status = 'hidden' WHERE id = :i"), {"i": hidden}
        )
    async with session(worker_engine, alpha) as connection:
        gone = await connection.execute(text("DELETE FROM items WHERE id = :i"), {"i": hidden})
        assert gone.rowcount == 0  # the row is not visible to the delete
        removed = await connection.execute(text("DELETE FROM items WHERE id = :i"), {"i": fresh})
        assert removed.rowcount == 1  # control: retention can delete ordinary items
    async with session(worker_engine, alpha) as connection:
        with pytest.raises(DBAPIError, match="row-level security"):
            await add_item(connection, alpha, url="https://example.com/sneaky", status="hidden")
    async with session(worker_engine, alpha) as connection:
        await add_item(connection, alpha, url="https://example.com/ok-new")  # controls
        await add_item(
            connection,
            alpha,
            url="https://example.com/ok-held",
            status="held",
            hold_reason="manual",
        )
    async with owner_engine.connect() as connection:
        still = (
            await connection.execute(text("SELECT status FROM items WHERE id = :i"), {"i": hidden})
        ).scalar_one()
    assert still == "hidden"


# --- links ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a b",
        "https://example.com/a\nb",
        "https://example.com/a\tb",
        "https://example.com/\r",
        'https://example.com/"onmouseover="x',
        "https://example.com/<script>",
        "https://example.com/a\x00b",
        "https://",
        "https:// example.com",
    ],
)
async def test_a_link_with_white_space_quotes_or_markup_is_refused(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org], url: str
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.connect() as connection:
        with pytest.raises((IntegrityError, DBAPIError)):
            await add_item(connection, alpha, url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a%20b?x=1&y=2#frag",
        "https://bücher.example/ärger",
        "http://[2606:4700:4700::1111]/x",
        "https://example.com:8443/path",
    ],
)
async def test_ordinary_links_are_accepted(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org], url: str
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        await add_item(connection, alpha, url=url)


# --- the exact privileges ------------------------------------------------------------------------


async def privileges(
    engine: AsyncEngine, role: str
) -> tuple[set[tuple[str, str]], set[tuple[str, str, str]]]:
    tables = ("sources", "fetch_runs", "raw_items", "items", "item_sources")
    async with engine.connect() as connection:
        table_rows = (
            await connection.execute(
                text(
                    "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                    "WHERE grantee = :r AND table_name = ANY(:t)"
                ),
                {"r": role, "t": list(tables)},
            )
        ).all()
        column_rows = (
            await connection.execute(
                text(
                    "SELECT table_name, column_name, privilege_type "
                    "FROM information_schema.column_privileges "
                    "WHERE grantee = :r AND table_name = ANY(:t) AND privilege_type <> 'REFERENCES'"
                ),
                {"r": role, "t": list(tables)},
            )
        ).all()
    # Column rows also list the privileges that come from table-wide grants; keep only the columns
    # of tables where the role holds no table-wide privilege of that type.
    wide = {(t, p) for t, p in table_rows}
    return wide, {(t, c, p) for t, c, p in column_rows if (t, p) not in wide}


SOURCE_COLUMNS_FOR_WORKER = {
    "id", "org_id", "owner_user_id", "type", "name", "config", "dedupe_key", "enabled",
    "interval_seconds", "next_fetch_at", "lease_until", "lease_token", "etag", "last_modified",
    "consecutive_failures", "last_error_code", "last_fetched_at", "last_success_at", "created_at",
    "updated_at",
}  # fmt: skip


async def test_the_application_has_exactly_these_ingestion_privileges(
    owner_engine: AsyncEngine,
) -> None:
    wide, columns = await privileges(owner_engine, "pharos_app")
    assert wide == {
        ("sources", "SELECT"), ("sources", "DELETE"), ("fetch_runs", "SELECT"),
        ("items", "SELECT"), ("item_sources", "SELECT"),
    }  # fmt: skip
    assert columns == (
        {("sources", c, "INSERT") for c in (
            "org_id", "owner_user_id", "type", "name", "config", "dedupe_key", "enabled",
            "interval_seconds", "created_by")}
        | {("sources", c, "UPDATE") for c in (
            "name", "config", "dedupe_key", "enabled", "interval_seconds")}
        | {("items", c, "UPDATE") for c in ("status", "hold_reason")}
    )  # fmt: skip


async def test_the_worker_has_exactly_these_ingestion_privileges(
    owner_engine: AsyncEngine,
) -> None:
    wide, columns = await privileges(owner_engine, "pharos_worker")
    assert wide == {
        ("fetch_runs", "SELECT"), ("fetch_runs", "DELETE"), ("raw_items", "SELECT"),
        ("raw_items", "DELETE"), ("items", "SELECT"), ("items", "DELETE"), ("item_sources", "SELECT"),
    }  # fmt: skip
    assert columns == (
        {("sources", c, "SELECT") for c in SOURCE_COLUMNS_FOR_WORKER}
        | {("sources", c, "UPDATE") for c in (
            "next_fetch_at", "lease_until", "lease_token", "etag", "last_modified",
            "consecutive_failures", "last_error_code", "last_fetched_at", "last_success_at")}
        | {("fetch_runs", c, "INSERT") for c in ("org_id", "source_id", "outcome")}
        | {("fetch_runs", c, "UPDATE") for c in (
            "finished_at", "outcome", "error_code", "http_status", "duration_ms", "bytes_fetched",
            "items_seen", "items_new")}
        | {("raw_items", c, "INSERT") for c in (
            "org_id", "source_id", "external_id", "title", "excerpt", "url", "published_at",
            "content_sha256")}
        | {("raw_items", "processed_at", "UPDATE")}
        | {("items", c, "INSERT") for c in (
            "org_id", "owner_user_id", "canonical_url", "url_hash", "title", "excerpt", "lang",
            "published_at", "content_sha256", "status", "hold_reason")}
        | {("items", c, "UPDATE") for c in (
            "title", "excerpt", "lang", "published_at", "content_sha256")}
        | {("item_sources", c, "INSERT") for c in ("org_id", "item_id", "source_id")}
    )  # fmt: skip


# --- a role nobody thought of ----------------------------------------------------------------------


async def test_the_personal_row_policy_also_holds_for_a_role_added_later(
    database: TestDatabase, alpha_beta: tuple[Org, Org], owner_engine: AsyncEngine
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        await add_source(connection, alpha, key="shared")
        await add_source(connection, alpha, key="mine", owner=alpha.user)
    admin_url = make_url(__import__("os").environ["PHAROS_TEST_ADMIN_URL"]).set(
        database=database.name
    )
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text("CREATE ROLE probe_reporting NOLOGIN"))
            await connection.execute(text("GRANT SELECT ON sources TO probe_reporting"))
            await connection.execute(
                text("GRANT EXECUTE ON FUNCTION pharos_current_org() TO probe_reporting")
            )
            await connection.execute(
                text("GRANT EXECUTE ON FUNCTION pharos_current_user() TO probe_reporting")
            )
        async with admin.connect() as connection:
            await connection.execute(text("SET ROLE probe_reporting"))
            await connection.execute(
                text("SELECT set_config('app.org_id', :o, false)"), {"o": str(alpha.id)}
            )
            seen = (await connection.execute(text("SELECT count(*) FROM sources"))).scalar_one()
            await connection.execute(
                text("SELECT set_config('app.user_id', :u, false)"), {"u": str(alpha.user)}
            )
            seen_own = (await connection.execute(text("SELECT count(*) FROM sources"))).scalar_one()
            await connection.execute(text("RESET ROLE"))  # the pooled connection is reused below
        assert (seen, seen_own) == (1, 2)  # only the shared one, then the shared one and their own
    finally:
        async with admin.connect() as connection:
            await connection.execute(text("RESET ROLE"))
            await connection.execute(text("DROP OWNED BY probe_reporting"))
            await connection.execute(text("DROP ROLE probe_reporting"))
        await admin.dispose()
