"""Migration 0003: sources, fetch runs, raw items, items, item sources (M3-05).

What is proven here: tenant isolation for both database roles, keys that cannot cross organizations,
the constraints on everything a feed can influence, deduplication, the owner rule for personal feeds
(and that other people, administrators included, cannot see them), the exact privileges of the
application and the worker, and that the migration can be reverted and applied again.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from .conftest import TestDatabase
from .ingestion_support import (
    INGESTION_TABLES,
    Org,
    add_item,
    add_source,
    count,
    session,
    sha,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# --- tenant isolation ----------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["app", "worker"])
async def test_each_organization_sees_only_its_own_ingestion_rows(
    app_engine: AsyncEngine,
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    role: str,
) -> None:
    alpha, beta = alpha_beta
    async with owner_engine.begin() as connection:  # data is created by the owner (RLS aside)
        for org in alpha_beta:
            source = await add_source(connection, org, key="feed")
            item = await add_item(connection, org, url=f"https://example.com/{org.id}")
            await connection.execute(
                text("INSERT INTO item_sources (org_id, item_id, source_id) VALUES (:o, :i, :s)"),
                {"o": org.id, "i": item, "s": source},
            )
            await connection.execute(
                text("INSERT INTO fetch_runs (org_id, source_id) VALUES (:o, :s)"),
                {"o": org.id, "s": source},
            )
            await connection.execute(
                text(
                    "INSERT INTO raw_items (org_id, source_id, external_id, content_sha256) "
                    "VALUES (:o, :s, 'x', :h)"
                ),
                {"o": org.id, "s": source, "h": sha("x")},
            )
    engine = app_engine if role == "app" else worker_engine
    tables = (
        INGESTION_TABLES if role == "worker" else ("sources", "fetch_runs", "items", "item_sources")
    )
    async with session(engine, alpha) as connection:
        assert {t: await count(connection, t) for t in tables} == {t: 1 for t in tables}
    async with session(engine, beta) as connection:
        assert {t: await count(connection, t) for t in tables} == {t: 1 for t in tables}
    async with engine.begin() as connection:  # no organization named: nothing at all
        assert {t: await count(connection, t) for t in tables} == {t: 0 for t in tables}


async def test_an_organization_cannot_write_rows_for_another(
    app_engine: AsyncEngine,
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
) -> None:
    alpha, beta = alpha_beta
    async with owner_engine.begin() as connection:
        source = await add_source(connection, alpha, key="feed")
    async with session(app_engine, alpha) as connection:
        with pytest.raises(DBAPIError, match="row-level security"):
            await add_source(connection, beta, key="stolen")
    async with session(worker_engine, alpha) as connection:
        with pytest.raises(DBAPIError, match="row-level security"):
            await connection.execute(
                text("INSERT INTO fetch_runs (org_id, source_id) VALUES (:o, :s)"),
                {"o": beta.id, "s": source},
            )
    async with session(worker_engine, alpha) as connection:  # positive control: its own is fine
        await connection.execute(
            text("INSERT INTO fetch_runs (org_id, source_id) VALUES (:o, :s)"),
            {"o": alpha.id, "s": source},
        )


async def test_keys_cannot_point_into_another_organization(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    async with owner_engine.begin() as connection:
        beta_source = await add_source(connection, beta, key="feed")
        beta_item = await add_item(connection, beta, url="https://example.com/b")
    for statement, params in (
        ("INSERT INTO fetch_runs (org_id, source_id) VALUES (:a, :s)", {"s": beta_source}),
        (
            "INSERT INTO raw_items (org_id, source_id, external_id, content_sha256) "
            "VALUES (:a, :s, 'x', :h)",
            {"s": beta_source, "h": sha("x")},
        ),
        (
            "INSERT INTO item_sources (org_id, item_id, source_id) VALUES (:a, :i, :s)",
            {"i": beta_item, "s": beta_source},
        ),
        (
            "INSERT INTO sources (org_id, owner_user_id, type, name, dedupe_key) "
            "VALUES (:a, :u, 'rss', 'x', 'k')",
            {"u": beta.user},
        ),
        (
            "INSERT INTO items (org_id, owner_user_id, canonical_url, url_hash, title, content_sha256) "
            "VALUES (:a, :u, 'https://example.com/z', :h, 't', :h)",
            {"u": beta.user, "h": sha("z")},
        ),
    ):
        async with owner_engine.connect() as connection:
            with pytest.raises(IntegrityError, match="foreign key"):
                await connection.execute(text(statement), {"a": alpha.id, **params})


# --- constraints on what a feed can influence ----------------------------------------------------


@pytest.mark.parametrize(
    ("column", "good", "bad", "constraint"),
    [
        ("type", "github", "ftp", "sources_type_check"),
        ("name", "n" * 200, "n" * 201, "sources_name_check"),
        ("name", "n", "", "sources_name_check"),
        ("interval_seconds", 300, 299, "sources_interval_check"),
        ("interval_seconds", 604800, 604801, "sources_interval_check"),
        ("consecutive_failures", 0, -1, "sources_failures_check"),
        ("last_error_code", "timeout", "Not A Code", "sources_error_code_check"),
        ("last_error_code", "blocked", "x" * 65, "sources_error_code_check"),
        ("etag", "e" * 512, "e" * 513, "sources_validators_check"),
        ("last_modified", "m" * 128, "m" * 129, "sources_validators_check"),
        ("dedupe_key", "k" * 128, "k" * 129, "sources_key_check"),
        ("config", '{"a": 1}', "[1, 2]", "sources_config_check"),
        ("config", '{"a": 1}', '{"a": "' + "x" * 9000 + '"}', "sources_config_check"),
    ],
)
async def test_source_values_are_limited(
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    column: str,
    good: object,
    bad: object,
    constraint: str,
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        await add_source(connection, alpha, key="good", **{column: good})  # the limit itself passes
    async with owner_engine.connect() as connection:
        with pytest.raises(IntegrityError, match=constraint):
            await add_source(connection, alpha, key="bad", **{column: bad})


async def test_a_lease_has_both_a_token_and_an_end(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        await add_source(
            connection,
            alpha,
            key="leased",
            lease_token=uuid.uuid4(),
            lease_until=datetime(2030, 1, 1, tzinfo=UTC),
        )
    async with owner_engine.connect() as connection:
        with pytest.raises(IntegrityError, match="sources_lease_check"):
            await add_source(connection, alpha, key="half", lease_token=uuid.uuid4())


@pytest.mark.parametrize(
    ("column", "good", "bad", "constraint"),
    [
        ("title", "t" * 300, "t" * 301, "items_title_check"),
        ("title", "t", "", "items_title_check"),
        ("excerpt", "e" * 500, "e" * 501, "items_excerpt_check"),
        ("lang", "az", "AZ", "items_lang_check"),
        ("lang", "fil", "english", "items_lang_check"),
        ("status", "hidden", "deleted", "items_status_check"),
        ("url_hash", "a" * 64, "A" * 64, "items_hash_check"),
        ("url_hash", "0" * 64, "0" * 63, "items_hash_check"),
        ("content_sha256", "b" * 64, "g" * 64, "items_content_hash_check"),
    ],
)
async def test_item_values_are_limited(
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    column: str,
    good: object,
    bad: object,
    constraint: str,
) -> None:
    alpha, _ = alpha_beta
    extra = {"url_hash": sha("other")} if column != "url_hash" else {}
    async with owner_engine.begin() as connection:
        await add_item(connection, alpha, url="https://example.com/good", **{column: good})
    async with owner_engine.connect() as connection:
        with pytest.raises(IntegrityError, match=constraint):
            await add_item(
                connection, alpha, url="https://example.com/bad", **{**extra, column: bad}
            )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>1</script>",
        "ftp://example.com/x",
        "file:///etc/passwd",
        "//example.com/x",
        "example.com/x",
        "HTTPS_://example.com",
    ],
)
async def test_an_item_link_must_be_http_or_https(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org], url: str
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        await add_item(connection, alpha, url="HTTPS://Example.com/ok")  # positive control
        await add_item(connection, alpha, url="http://example.com/ok")
    async with owner_engine.connect() as connection:
        with pytest.raises(IntegrityError, match="items_url_check"):
            await add_item(connection, alpha, url=url)


async def test_a_long_item_link_is_refused_at_the_limit(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    prefix = "https://example.com/"
    async with owner_engine.begin() as connection:
        await add_item(connection, alpha, url=prefix + "a" * (2048 - len(prefix)))
    async with owner_engine.connect() as connection:
        with pytest.raises(IntegrityError, match="items_url_check"):
            await add_item(connection, alpha, url=prefix + "b" * (2049 - len(prefix)))


@pytest.mark.parametrize(
    ("status", "reason", "ok"),
    [
        ("held", "suspicious_link", True),
        ("held", None, False),
        ("new", "suspicious_link", False),
        ("hidden", None, True),
        ("held", "Has Spaces", False),
        ("held", "x" * 65, False),
    ],
)
async def test_a_held_item_always_says_why_and_only_held_items_do(
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    status: str,
    reason: str | None,
    ok: bool,
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.connect() as connection:
        try:
            await add_item(
                connection, alpha, url="https://example.com/h", status=status, hold_reason=reason
            )
        except IntegrityError as error:
            assert not ok and "items_hold_check" in str(error)
        else:
            assert ok
        finally:
            await connection.rollback()


async def test_raw_item_values_are_limited(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        source = await add_source(connection, alpha, key="feed")
    insert = (
        "INSERT INTO raw_items (org_id, source_id, external_id, title, excerpt, url, content_sha256) "
        "VALUES (:o, :s, :e, :t, :x, :u, :h)"
    )
    good = {
        "o": alpha.id,
        "s": source,
        "e": "e" * 512,
        "t": "t" * 300,
        "x": "x" * 500,
        "u": "https://example.com/" + "u" * 100,
        "h": sha("x"),
    }
    async with owner_engine.begin() as connection:
        await connection.execute(text(insert), good)
    for change, constraint in (
        ({"e": ""}, "raw_items_external_check"),
        ({"e": "e" * 513}, "raw_items_external_check"),
        ({"t": "t" * 301}, "raw_items_title_check"),
        ({"x": "x" * 501}, "raw_items_excerpt_check"),
        ({"u": "javascript:alert(1)"}, "raw_items_url_check"),
        ({"h": "short"}, "raw_items_hash_check"),
    ):
        async with owner_engine.connect() as connection:
            with pytest.raises(IntegrityError, match=constraint):
                await connection.execute(text(insert), {**good, "e": "second", **change})
    async with owner_engine.connect() as connection:  # the same external id twice is refused
        with pytest.raises(IntegrityError, match="raw_items_external_key"):
            await connection.execute(text(insert), good)


async def test_fetch_run_values_are_limited(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        source = await add_source(connection, alpha, key="feed")
    insert = (
        "INSERT INTO fetch_runs (org_id, source_id, outcome, error_code, http_status, duration_ms) "
        "VALUES (:o, :s, :outcome, :code, :status, :ms)"
    )
    good = {"o": alpha.id, "s": source, "outcome": "ok", "code": None, "status": 200, "ms": 5}
    async with owner_engine.begin() as connection:
        await connection.execute(text(insert), good)
    for change, constraint in (
        ({"outcome": "great"}, "fetch_runs_outcome_check"),
        ({"code": "Bad Code"}, "fetch_runs_error_check"),
        ({"status": 99}, "fetch_runs_status_check"),
        ({"status": 600}, "fetch_runs_status_check"),
        ({"ms": -1}, "fetch_runs_counts_check"),
    ):
        async with owner_engine.connect() as connection:
            with pytest.raises(IntegrityError, match=constraint):
                await connection.execute(text(insert), {**good, **change})


# --- deduplication -------------------------------------------------------------------------------


async def test_a_source_cannot_be_added_twice(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    async with owner_engine.begin() as connection:
        await add_source(connection, alpha, key="same")
        await add_source(connection, beta, key="same")  # another organization: fine
        await add_source(connection, alpha, key="same", owner=alpha.user)  # a person's own: fine
        await add_source(connection, alpha, key="same", owner=alpha.other_user)
    for owner in (None, alpha.user):
        async with owner_engine.connect() as connection:
            with pytest.raises(IntegrityError, match="sources_dedupe_key"):
                await add_source(connection, alpha, key="same", owner=owner)


async def test_an_organization_item_exists_once_and_a_personal_item_never_merges_into_it(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    url = "https://example.com/article"
    async with owner_engine.begin() as connection:
        org_item = await add_item(connection, alpha, url=url)
        personal = await add_item(connection, alpha, url=url, owner=alpha.user)
        await add_item(connection, alpha, url=url, owner=alpha.other_user)
        await add_item(connection, beta, url=url)
    assert org_item != personal
    for owner in (None, alpha.user):  # NULL owners count as equal: the organization item is unique
        async with owner_engine.connect() as connection:
            with pytest.raises(IntegrityError, match="items_url_key"):
                await add_item(connection, alpha, url=url, owner=owner)


async def test_one_item_seen_in_two_sources_is_one_item(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        first = await add_source(connection, alpha, key="one")
        second = await add_source(connection, alpha, key="two")
        item = await add_item(connection, alpha, url="https://example.com/shared")
        for source in (first, second):
            await connection.execute(
                text("INSERT INTO item_sources (org_id, item_id, source_id) VALUES (:o, :i, :s)"),
                {"o": alpha.id, "i": item, "s": source},
            )
    async with owner_engine.connect() as connection:
        with pytest.raises(IntegrityError, match="item_sources_pkey"):
            await connection.execute(
                text("INSERT INTO item_sources (org_id, item_id, source_id) VALUES (:o, :i, :s)"),
                {"o": alpha.id, "i": item, "s": first},
            )
    async with owner_engine.connect() as connection:
        assert await count(connection, "items") == 1
        assert await count(connection, "item_sources") == 2


# --- personal feeds ------------------------------------------------------------------------------


async def test_an_item_and_its_source_must_have_the_same_owner(
    owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        org_source = await add_source(connection, alpha, key="org")
        mine = await add_source(connection, alpha, key="mine", owner=alpha.user)
        org_item = await add_item(connection, alpha, url="https://example.com/o")
        my_item = await add_item(connection, alpha, url="https://example.com/m", owner=alpha.user)
        theirs = await add_item(
            connection, alpha, url="https://example.com/t", owner=alpha.other_user
        )
        for item, source in ((org_item, org_source), (my_item, mine)):  # positive controls
            await connection.execute(
                text("INSERT INTO item_sources (org_id, item_id, source_id) VALUES (:o, :i, :s)"),
                {"o": alpha.id, "i": item, "s": source},
            )
    for item, source in ((org_item, mine), (my_item, org_source), (theirs, mine)):
        async with owner_engine.connect() as connection:
            with pytest.raises(IntegrityError, match="same owner"):
                await connection.execute(
                    text(
                        "INSERT INTO item_sources (org_id, item_id, source_id) VALUES (:o, :i, :s)"
                    ),
                    {"o": alpha.id, "i": item, "s": source},
                )


async def test_personal_rows_are_hidden_from_everybody_else_even_administrators(
    app_engine: AsyncEngine,
    worker_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        shared = await add_source(connection, alpha, key="shared")
        mine = await add_source(connection, alpha, key="mine", owner=alpha.user)
        theirs = await add_source(connection, alpha, key="theirs", owner=alpha.other_user)
        shared_item = await add_item(connection, alpha, url="https://example.com/s")
        my_item = await add_item(connection, alpha, url="https://example.com/m", owner=alpha.user)
        their_item = await add_item(
            connection, alpha, url="https://example.com/t", owner=alpha.other_user
        )
        for item, source in ((shared_item, shared), (my_item, mine), (their_item, theirs)):
            await connection.execute(
                text("INSERT INTO item_sources (org_id, item_id, source_id) VALUES (:o, :i, :s)"),
                {"o": alpha.id, "i": item, "s": source},
            )
            await connection.execute(
                text("INSERT INTO fetch_runs (org_id, source_id) VALUES (:o, :s)"),
                {"o": alpha.id, "s": source},
            )

    async def seen(engine: AsyncEngine, user: uuid.UUID | None) -> dict[str, int]:
        async with session(engine, alpha, user) as connection:
            return {
                t: await count(connection, t)
                for t in ("sources", "items", "item_sources", "fetch_runs")
            }

    everything = {"sources": 3, "items": 3, "item_sources": 3, "fetch_runs": 3}
    organization_only = {"sources": 1, "items": 1, "item_sources": 1, "fetch_runs": 1}
    with_own = {"sources": 2, "items": 2, "item_sources": 2, "fetch_runs": 2}
    assert await seen(app_engine, None) == organization_only  # no person named: only shared rows
    assert await seen(app_engine, alpha.user) == with_own
    assert await seen(app_engine, alpha.other_user) == with_own
    assert await seen(app_engine, uuid.uuid4()) == organization_only  # someone else entirely
    assert await seen(worker_engine, None) == everything  # the worker processes every feed
    # A person cannot create or change a row for somebody else either.
    async with session(app_engine, alpha, alpha.user) as connection:
        with pytest.raises(DBAPIError, match="row-level security"):
            await add_source(connection, alpha, key="forged", owner=alpha.other_user)
    async with session(app_engine, alpha, alpha.user) as connection:
        await add_source(connection, alpha, key="legit", owner=alpha.user)  # positive control
        updated = await connection.execute(
            text("UPDATE sources SET enabled = true WHERE id = :id"), {"id": theirs}
        )
        assert updated.rowcount == 0


# --- privileges ------------------------------------------------------------------------------------


async def test_the_application_can_manage_sources_and_review_items_but_nothing_else(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with session(app_engine, alpha) as connection:
        source = await add_source(connection, alpha, key="feed", enabled=True)
        await connection.execute(
            text("UPDATE sources SET name = 'Renamed', interval_seconds = 900 WHERE id = :id"),
            {"id": source},
        )
    async with owner_engine.begin() as connection:
        item = await add_item(connection, alpha, url="https://example.com/a")
    async with session(app_engine, alpha) as connection:
        await connection.execute(
            text("UPDATE items SET status = 'held', hold_reason = 'manual' WHERE id = :id"),
            {"id": item},
        )
    for statement in (
        "UPDATE sources SET etag = 'x'",
        "UPDATE sources SET lease_token = gen_random_uuid(), lease_until = now()",
        "UPDATE sources SET consecutive_failures = 5",
        "UPDATE sources SET last_error_code = 'x'",
        "UPDATE sources SET type = 'github'",
        "UPDATE sources SET org_id = gen_random_uuid()",
        "UPDATE items SET title = 'edited by an administrator'",
        "UPDATE items SET canonical_url = 'https://evil.example/'",
        "UPDATE items SET url_hash = repeat('c', 64)",
        "DELETE FROM items",
        "INSERT INTO raw_items (org_id, source_id, external_id, content_sha256) "
        "SELECT org_id, id, 'x', repeat('d', 64) FROM sources",
        "SELECT 1 FROM raw_items",
        "INSERT INTO fetch_runs (org_id, source_id) SELECT org_id, id FROM sources",
        "UPDATE fetch_runs SET outcome = 'ok'",
        "INSERT INTO item_sources (org_id, item_id, source_id) SELECT org_id, id, id FROM items",
    ):
        async with session(app_engine, alpha) as connection:
            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(text(statement))
    async with session(app_engine, alpha) as connection:
        await connection.execute(text("DELETE FROM sources WHERE id = :id"), {"id": source})


async def test_the_worker_can_run_the_pipeline_and_nothing_beyond_it(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        source = await add_source(connection, alpha, key="feed", enabled=True)
        await add_item(connection, alpha, url="https://example.com/seed")
    token = uuid.uuid4()
    async with session(worker_engine, alpha) as connection:
        due = (
            (
                await connection.execute(
                    text(
                        "SELECT id FROM sources WHERE enabled AND next_fetch_at <= now() "
                        "ORDER BY next_fetch_at FOR UPDATE SKIP LOCKED"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert due == [source]
        await connection.execute(
            text(
                "UPDATE sources SET lease_token = :t, lease_until = now() + interval '5 minutes' "
                "WHERE id = :id"
            ),
            {"t": token, "id": source},
        )
        run = (
            await connection.execute(
                text(
                    "INSERT INTO fetch_runs (org_id, source_id, outcome) "
                    "VALUES (:o, :s, 'running') RETURNING id"
                ),
                {"o": alpha.id, "s": source},
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO raw_items (org_id, source_id, external_id, title, content_sha256) "
                "VALUES (:o, :s, 'guid-1', 'Hello', :h)"
            ),
            {"o": alpha.id, "s": source, "h": sha("hello")},
        )
        item = await add_item(
            connection,
            alpha,
            url="https://example.com/new",
            status="held",
            hold_reason="suspicious_link",
        )
        await connection.execute(
            text("INSERT INTO item_sources (org_id, item_id, source_id) VALUES (:o, :i, :s)"),
            {"o": alpha.id, "i": item, "s": source},
        )
        await connection.execute(
            text("UPDATE raw_items SET processed_at = now() WHERE external_id = 'guid-1'")
        )
        await connection.execute(
            text(
                "UPDATE fetch_runs SET finished_at = now(), outcome = 'ok', items_seen = 1, "
                "items_new = 1 WHERE id = :id"
            ),
            {"id": run},
        )
        await connection.execute(
            text(
                "UPDATE sources SET lease_token = NULL, lease_until = NULL, etag = 'v1', "
                "consecutive_failures = 0, last_success_at = now(), "
                "next_fetch_at = now() + interval '1 hour' WHERE id = :id"
            ),
            {"id": source},
        )
        await connection.execute(
            text("UPDATE items SET excerpt = 'refreshed' WHERE id = :id"), {"id": item}
        )
        # Retention.
        await connection.execute(text("DELETE FROM raw_items WHERE fetched_at < now()"))
        await connection.execute(
            text("DELETE FROM fetch_runs WHERE started_at < now() - interval '30 days'")
        )
    for statement in (
        "INSERT INTO sources (org_id, type, name, dedupe_key) VALUES (gen_random_uuid(), 'rss', 'x', 'y')",
        "UPDATE sources SET enabled = true",
        "UPDATE sources SET config = '{}'::jsonb",
        "UPDATE sources SET interval_seconds = 300",
        "UPDATE sources SET type = 'github'",
        "UPDATE sources SET owner_user_id = NULL",
        "DELETE FROM sources",
        "UPDATE items SET status = 'new', hold_reason = NULL",  # a person's decision stays
        "UPDATE items SET canonical_url = 'https://evil.example/'",
        "UPDATE items SET owner_user_id = NULL",
        "UPDATE fetch_runs SET source_id = gen_random_uuid()",
        "UPDATE raw_items SET external_id = 'other'",
        "UPDATE item_sources SET item_id = item_id",
        "DELETE FROM item_sources",
        "SELECT email FROM users",
        "SELECT role FROM users",
        "UPDATE users SET status = 'active'",
        "SELECT 1 FROM organizations",
    ):
        async with session(worker_engine, alpha) as connection:
            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(text(statement))


async def test_a_visible_decision_survives_a_later_fetch(
    worker_engine: AsyncEngine,
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        item = await add_item(connection, alpha, url="https://example.com/decide")
    async with session(app_engine, alpha) as connection:
        await connection.execute(
            text("UPDATE items SET status = 'hidden' WHERE id = :i"), {"i": item}
        )
    async with session(worker_engine, alpha) as connection:  # the refresh changes the text only
        await connection.execute(
            text("UPDATE items SET title = 'Newer title' WHERE id = :i"), {"i": item}
        )
        status = (
            await connection.execute(text("SELECT status FROM items WHERE id = :i"), {"i": item})
        ).scalar_one()
    assert status == "hidden"


# --- behaviour -----------------------------------------------------------------------------------


async def test_updated_at_moves_on_update(
    owner_engine: AsyncEngine, app_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        source = await add_source(connection, alpha, key="feed")
        item = await add_item(connection, alpha, url="https://example.com/u")
        await connection.execute(
            text("UPDATE sources SET updated_at = '2020-01-01' WHERE id = :i"), {"i": source}
        )
        await connection.execute(
            text("UPDATE items SET updated_at = '2020-01-01' WHERE id = :i"), {"i": item}
        )
    async with session(app_engine, alpha) as connection:
        await connection.execute(text("UPDATE sources SET name = 'x' WHERE id = :i"), {"i": source})
        await connection.execute(
            text("UPDATE items SET status = 'hidden' WHERE id = :i"), {"i": item}
        )
        for table, row in (("sources", source), ("items", item)):
            moved = await connection.execute(
                text(f"SELECT updated_at > '2021-01-01' FROM {table} WHERE id = :i"),
                {"i": row},
            )
            assert moved.scalar_one() is True


async def test_removing_a_source_removes_its_history_but_not_the_items(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        source = await add_source(connection, alpha, key="feed")
        item = await add_item(connection, alpha, url="https://example.com/kept")
        await connection.execute(
            text("INSERT INTO item_sources (org_id, item_id, source_id) VALUES (:o, :i, :s)"),
            {"o": alpha.id, "i": item, "s": source},
        )
        await connection.execute(
            text("INSERT INTO fetch_runs (org_id, source_id) VALUES (:o, :s)"),
            {"o": alpha.id, "s": source},
        )
        await connection.execute(
            text(
                "INSERT INTO raw_items (org_id, source_id, external_id, content_sha256) VALUES (:o, :s, 'g', :h)"
            ),
            {"o": alpha.id, "s": source, "h": sha("g")},
        )
    async with session(app_engine, alpha) as connection:
        await connection.execute(text("DELETE FROM sources WHERE id = :i"), {"i": source})
    async with owner_engine.connect() as connection:
        remaining = {t: await count(connection, t) for t in INGESTION_TABLES}
    assert remaining == {
        "sources": 0,
        "fetch_runs": 0,
        "raw_items": 0,
        "items": 1,
        "item_sources": 0,
    }


# --- the migration itself ----------------------------------------------------------------------


async def test_migration_0003_can_be_reverted_and_applied_again(
    database: TestDatabase, owner_engine: AsyncEngine
) -> None:
    async def tables() -> set[str]:
        async with owner_engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {row[0] for row in rows}

    async def grants() -> set[tuple[str, str, str]]:
        async with owner_engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT table_name, grantee, privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_schema = 'public' AND grantee IN ('pharos_app', 'pharos_worker') "
                    "AND table_name = ANY(:t)"
                ),
                {"t": list(INGESTION_TABLES)},
            )
            return {tuple(row) for row in rows}

    before = await grants()
    assert before and set(INGESTION_TABLES) <= await tables()
    try:
        await asyncio.to_thread(command.downgrade, database.alembic, "0002")
        assert not (set(INGESTION_TABLES) & await tables())
        async with owner_engine.connect() as connection:
            names = {
                row[0]
                for row in await connection.execute(
                    text("SELECT proname FROM pg_proc WHERE proname LIKE 'pharos\\_%'")
                )
            }
        assert "pharos_current_user" not in names and "pharos_check_item_source_owner" not in names
    finally:
        await asyncio.to_thread(command.upgrade, database.alembic, "head")
    assert set(INGESTION_TABLES) <= await tables()
    assert await grants() == before


async def test_downgrade_refuses_to_delete_ingested_data_unless_told_to(
    database: TestDatabase,
    owner_engine: AsyncEngine,
    alpha_beta: tuple[Org, Org],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha, _ = alpha_beta
    async with owner_engine.begin() as connection:
        await add_source(connection, alpha, key="feed")
    monkeypatch.delenv("PHAROS_ALLOW_DATA_LOSS", raising=False)
    with pytest.raises(RuntimeError, match="PHAROS_ALLOW_DATA_LOSS"):
        await asyncio.to_thread(command.downgrade, database.alembic, "0002")
    async with owner_engine.connect() as connection:
        assert await count(connection, "sources") == 1
        revision = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    assert revision == "0005"  # the refused downgrade rolled everything back
    monkeypatch.setenv("PHAROS_ALLOW_DATA_LOSS", "yes")
    try:
        await asyncio.to_thread(command.downgrade, database.alembic, "0002")
    finally:
        monkeypatch.delenv("PHAROS_ALLOW_DATA_LOSS")
        await asyncio.to_thread(command.upgrade, database.alembic, "head")
    async with owner_engine.connect() as connection:
        assert await count(connection, "sources") == 0


async def test_migration_0004_takes_the_workers_view_of_accounts_away_when_reverted(
    database: TestDatabase, owner_engine: AsyncEngine
) -> None:
    async def worker_reads_status() -> bool:
        async with owner_engine.connect() as connection:
            row = await connection.execute(
                text(
                    "SELECT has_column_privilege('pharos_worker', 'public.users', 'status', 'SELECT')"
                )
            )
            return bool(row.scalar_one())

    assert await worker_reads_status() is True
    await asyncio.to_thread(command.downgrade, database.alembic, "0003")
    try:
        assert await worker_reads_status() is False
    finally:
        await asyncio.to_thread(command.upgrade, database.alembic, "head")
    assert await worker_reads_status() is True
