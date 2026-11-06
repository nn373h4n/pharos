"""Retention and deleting one person, against real PostgreSQL and Redis (M5-09, ADR-0025): what the
three operator commands remove, what they leave, the day a boundary falls on, and that nobody else
can do it."""

# ruff: noqa: F811  (fixtures are imported by name)

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from pharos import cli, retention
from pharos.core.config import OperatorSettings
from pharos.core.errors import DependencyUnavailable
from pharos.core.sessions import SessionStore
from pharos.retention import RetentionError

from .conftest import TestDatabase
from .test_local_auth import MakeStack, Stack, audit_rows, make_stack  # noqa: F401
from .test_sources_api import _remove_what_the_test_made  # noqa: F401
from .test_user_admin import Person, person

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def slug_of(stack: Stack, org: uuid.UUID | None = None) -> str:
    async with stack.owner.connect() as connection:
        return str(
            (
                await connection.execute(
                    text("SELECT slug FROM organizations WHERE id = :o"), {"o": org or stack.org_a}
                )
            ).scalar_one()
        )


async def one(stack: Stack, sql: str, **params: Any) -> Any:
    async with stack.owner.connect() as connection:
        return (await connection.execute(text(sql), params)).scalar_one()


async def article(
    stack: Stack, org: uuid.UUID, name: str, owner: uuid.UUID | None = None
) -> uuid.UUID:
    async with stack.owner.begin() as connection:
        return (
            await connection.execute(
                text(
                    "INSERT INTO items (org_id, owner_user_id, canonical_url, url_hash, title, "
                    "content_sha256) VALUES (:o, :u, :url, :h, :t, :c) RETURNING id"
                ),
                {
                    "o": org,
                    "u": owner,
                    "url": f"https://news.example.org/{name}",
                    "h": sha("u" + name),
                    "t": name,
                    "c": sha("c" + name),
                },
            )
        ).scalar_one()


async def interaction(
    stack: Stack,
    user: uuid.UUID,
    item: uuid.UUID,
    kind: str,
    when: datetime,
    value: int | None = None,
    org: uuid.UUID | None = None,
) -> None:
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO interactions (org_id, user_id, item_id, type, value, created_at) "
                "VALUES (:o, :u, :i, :t, :v, :w)"
            ),
            {"o": org or stack.org_a, "u": user, "i": item, "t": kind, "v": value, "w": when},
        )


async def members(stack: Stack, count: int, org: uuid.UUID | None = None) -> list[uuid.UUID]:
    """Plain accounts, made directly (no sign-in needed)."""
    found: list[uuid.UUID] = []
    async with stack.owner.begin() as connection:
        for _ in range(count):
            found.append(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO users (org_id, issuer, subject, display_name, role) "
                            "VALUES (:o, 'local', :s, 'Someone', 'member') RETURNING id"
                        ),
                        {"o": org or stack.org_a, "s": f"m-{uuid.uuid4().hex[:10]}"},
                    )
                ).scalar_one()
            )
    return found


async def personal_feed(stack: Stack, owner: uuid.UUID) -> uuid.UUID:
    async with stack.owner.begin() as connection:
        source = (
            await connection.execute(
                text(
                    "INSERT INTO sources (org_id, owner_user_id, type, name, config, dedupe_key, "
                    "interval_seconds) VALUES (:o, :u, 'rss', 'Mine', "
                    'CAST(\'{"url": "https://mine.example.org/feed"}\' AS jsonb), :k, 3600) '
                    "RETURNING id"
                ),
                {"o": stack.org_a, "u": owner, "k": uuid.uuid4().hex},
            )
        ).scalar_one()
    return uuid.UUID(str(source))


def operator(stack: Stack, database: TestDatabase, slug: str) -> OperatorSettings:
    return OperatorSettings(
        migrations_database_url=database.owner_url,  # type: ignore[arg-type]
        org_slug=slug,
        redis_url=stack.settings.redis_url,
        session_redis_db=stack.settings.session_redis_db,
        session_idle_seconds=stack.settings.session_idle_seconds,
        session_absolute_seconds=stack.settings.session_absolute_seconds,
    )


def details_of(entry: dict[str, Any]) -> dict[str, Any]:
    raw = entry["details"]
    return raw if isinstance(raw, dict) else json.loads(raw)


def entries(rows: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    return [r for r in rows if r["action"] == action]


# --- deleting a person -------------------------------------------------------------------------------


async def prepared(stack: Stack) -> tuple[Person, Person, Person, uuid.UUID]:
    """An administrator who stays, a member who will be deleted, and another member who must not
    be touched; the member has everything that can belong to a person."""
    admin = await person(stack, "admin")
    target = await person(stack, "member")
    other = await person(stack, "member")
    shared = await article(stack, stack.org_a, "shared")
    for who in (target, other):
        await interaction(stack, who.id, shared, "view", NOW - timedelta(days=1))
        await interaction(stack, who.id, shared, "save", NOW - timedelta(days=1))
    feed = await personal_feed(stack, target.id)
    await article(stack, stack.org_a, "mine-1", owner=target.id)
    await article(stack, stack.org_a, "mine-2", owner=target.id)
    await personal_feed(stack, other.id)
    return admin, target, other, feed


async def test_nothing_changes_until_the_deletion_is_confirmed(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    _, target, _, _ = await prepared(stack)
    result = await retention.delete_person(
        database.owner_url, org_slug=await slug_of(stack), user_id=target.id
    )
    assert (result.interactions, result.personal_feeds, result.personal_articles) == (2, 1, 2)
    assert result.deleted is False
    assert await one(stack, "SELECT count(*) FROM users WHERE id = :i", i=target.id) == 1
    assert (
        await one(stack, "SELECT count(*) FROM interactions WHERE user_id = :i", i=target.id) == 2
    )
    assert entries(await audit_rows(stack), "user.deleted") == []
    # and the person is still signed in
    assert (await target.client.get("/api/me")).status_code == 200


async def test_deleting_a_person_removes_everything_that_is_theirs_and_nothing_else(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    admin, target, other, _ = await prepared(stack)
    result = await retention.delete_person(
        database.owner_url, org_slug=await slug_of(stack), user_id=target.id, confirm=True
    )
    assert result.deleted and (result.interactions, result.personal_feeds) == (2, 1)
    mine = {"i": target.id}
    for table, column in (
        ("users", "id"),
        ("user_profiles", "user_id"),
        ("local_credentials", "user_id"),
        ("interactions", "user_id"),
        ("sources", "owner_user_id"),
        ("items", "owner_user_id"),
    ):
        assert await one(stack, f"SELECT count(*) FROM {table} WHERE {column} = :i", **mine) == 0, (
            table
        )
    # Another person, the administrator and what belongs to everybody are untouched.
    assert (
        await one(
            stack, "SELECT count(*) FROM users WHERE id = ANY(:ids)", ids=[admin.id, other.id]
        )
        == 2
    )
    assert await one(stack, "SELECT count(*) FROM interactions WHERE user_id = :i", i=other.id) == 2
    assert (
        await one(stack, "SELECT count(*) FROM sources WHERE owner_user_id = :i", i=other.id) == 1
    )
    assert await one(stack, "SELECT count(*) FROM items WHERE title = 'shared'") == 1
    # The deleted person's session is useless at once: the account is gone.
    assert (await target.client.get("/api/me")).status_code == 401
    assert (await other.client.get("/api/me")).status_code == 200


async def test_the_one_trace_is_an_audit_entry_that_holds_counts_and_no_name(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    _, target, _, _ = await prepared(stack)
    before = await audit_rows(stack)
    await retention.delete_person(
        database.owner_url, org_slug=await slug_of(stack), username=target.username, confirm=True
    )
    after = await audit_rows(stack)
    made = entries(after, "user.deleted")
    assert len(made) == 1 and made[0]["outcome"] == "success"
    assert made[0]["actor_type"] == "operator_cli" and made[0]["target"] == f"user:{target.id}"
    assert details_of(made[0]) == {
        "interactions": 2,
        "personal_feeds": 1,
        "personal_articles": 2,
        "audit_rows_cleared": 1,  # the one sign-in of the person, written by the test's login
        "marks_cleared": 0,
        "was_administrator": False,
    }
    # No name, no username and no address anywhere in what was added to the log.
    added = json.dumps([{k: str(v) for k, v in e.items()} for e in after[len(before) :]])
    assert target.username not in added and "example.org" not in added
    # What the person did before stays until the audit retention ends (it holds an identifier).
    assert len(after) > len(before)


async def test_the_last_active_administrator_is_not_deleted_but_one_of_two_is(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    only = await person(stack, "admin")
    slug = await slug_of(stack)
    with pytest.raises(RetentionError, match="last active administrator"):
        await retention.delete_person(
            database.owner_url, org_slug=slug, user_id=only.id, confirm=True
        )
    assert await one(stack, "SELECT count(*) FROM users WHERE id = :i", i=only.id) == 1
    second = await person(stack, "admin")
    result = await retention.delete_person(
        database.owner_url, org_slug=slug, user_id=only.id, confirm=True
    )
    assert result.was_administrator and result.deleted
    assert await one(stack, "SELECT count(*) FROM users WHERE id = :i", i=second.id) == 1


async def test_a_disabled_administrator_may_be_deleted_even_if_no_other_is_left(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    """The rule protects the ability to run the installation: a disabled administrator is not it."""
    stack: Stack = await make_stack("on")
    keeper = await person(stack, "admin")
    gone = await person(stack, "admin", status="disabled")
    result = await retention.delete_person(
        database.owner_url, org_slug=await slug_of(stack), user_id=gone.id, confirm=True
    )
    assert result.deleted
    assert await one(stack, "SELECT count(*) FROM users WHERE id = :i", i=keeper.id) == 1


async def test_a_person_of_another_organization_is_never_found(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    await person(stack, "admin")
    foreign = (await members(stack, 1, org=stack.org_b))[0]
    with pytest.raises(RetentionError, match="no such person"):
        await retention.delete_person(
            database.owner_url, org_slug=await slug_of(stack), user_id=foreign, confirm=True
        )
    assert await one(stack, "SELECT count(*) FROM users WHERE id = :i", i=foreign) == 1


@pytest.mark.parametrize("both", [True, False])
async def test_the_person_is_named_by_exactly_one_of_an_id_and_a_username(
    make_stack: MakeStack, database: TestDatabase, both: bool
) -> None:
    stack: Stack = await make_stack("on")
    slug = await slug_of(stack)
    kwargs: dict[str, Any] = {"user_id": uuid.uuid4(), "username": "x"} if both else {}
    with pytest.raises(RetentionError, match="exactly one"):
        await retention.delete_person(database.owner_url, org_slug=slug, confirm=True, **kwargs)


async def test_the_command_ends_the_sessions_and_says_so(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    await person(stack, "admin")
    target = await person(stack, "member")
    assert (await target.client.get("/api/me")).status_code == 200
    settings = operator(stack, database, await slug_of(stack))
    shown = await cli.delete_person(
        settings, username=target.username, user_id=None, confirm=False, allow_reprovision=False
    )
    assert "would delete" in shown and "Nothing was changed" in shown
    assert (await target.client.get("/api/me")).status_code == 200
    done = await cli.delete_person(
        settings, username=target.username, user_id=None, confirm=True, allow_reprovision=False
    )
    assert f"deleted person {target.id}" in done and "Ended 1 session" in done
    assert (await target.client.get("/api/me")).status_code == 401
    # The session record itself is gone from Redis, not only unusable.
    assert await one(stack, "SELECT count(*) FROM users WHERE id = :i", i=target.id) == 0


async def test_when_redis_is_down_the_person_is_still_deleted_and_the_command_says_what_is_left(
    make_stack: MakeStack, database: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack: Stack = await make_stack("on")
    await person(stack, "admin")
    target = await person(stack, "member")

    async def down(self: SessionStore, user_id: uuid.UUID) -> int:
        raise DependencyUnavailable("redis is down")

    monkeypatch.setattr(SessionStore, "revoke_user", down)
    done = await cli.delete_person(
        operator(stack, database, await slug_of(stack)),
        username=target.username,
        user_id=None,
        confirm=True,
        allow_reprovision=False,
    )
    assert "could not be ended" in done and "expire by themselves" in done
    assert await one(stack, "SELECT count(*) FROM users WHERE id = :i", i=target.id) == 0


async def test_a_deleted_persons_username_and_address_hash_are_cleared_from_the_audit_log(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    await person(stack, "admin")
    target = await person(stack, "member")
    bystander = await person(stack, "member")
    slug = await slug_of(stack)
    # A refused sign-in that named the person: no actor, the username as target, a hash.
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO audit_logs (org_id, actor_type, action, target, outcome, ip_hash) "
                "VALUES (:o, 'system', 'auth.login_refused', :t, 'failure', :h)"
            ),
            {"o": stack.org_a, "t": f"local:{target.username}", "h": sha("203.0.113.9")},
        )
    # Control: the sign-ins of both people left the username in a target and a hash on a row.
    before = await audit_rows(stack)
    assert any(e["target"] == f"local:{target.username}" for e in before)
    assert any(e["actor_id"] == target.id and e["ip_hash"] for e in before)
    assert any(e["actor_id"] is None and e["ip_hash"] for e in before)
    await retention.delete_person(
        database.owner_url, org_slug=slug, user_id=target.id, confirm=True
    )
    after = await audit_rows(stack)
    assert not any(target.username in str(e["target"]) for e in after)
    assert not any(e["actor_id"] == target.id and e["ip_hash"] for e in after)
    assert not any(e["actor_id"] is None and e["ip_hash"] for e in after)  # the refused one too
    # Their rows stay (the identifier is the trail), only the name and the address went.
    assert sum(1 for e in after if e["actor_id"] == target.id) >= 1
    assert any(e["target"] == "local:(deleted)" for e in after)
    # Somebody else's entries are untouched.
    assert [e for e in before if e["actor_id"] == bystander.id] == [
        e for e in after if e["actor_id"] == bystander.id
    ]
    assert any(e["target"] == f"local:{bystander.username}" for e in after)


async def test_the_created_by_marks_of_a_deleted_person_are_cleared_not_blocking(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    await person(stack, "admin")
    target = await person(stack, "member")
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO sources (org_id, type, name, config, dedupe_key, interval_seconds, "
                "created_by) VALUES (:o, 'rss', 'Shared', "
                'CAST(\'{"url": "https://shared.example.org/feed"}\' AS jsonb), :k, 3600, :u)'
            ),
            {"o": stack.org_a, "k": uuid.uuid4().hex, "u": target.id},
        )
    result = await retention.delete_person(
        database.owner_url, org_slug=await slug_of(stack), user_id=target.id, confirm=True
    )
    assert result.marks_cleared == 1
    # The shared source stays; only the mark is gone.
    assert await one(stack, "SELECT count(*) FROM sources WHERE name = 'Shared'") == 1
    assert await one(stack, "SELECT count(*) FROM sources WHERE created_by = :u", u=target.id) == 0


async def test_a_person_from_the_identity_provider_needs_the_reprovision_flag(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    await person(stack, "admin")
    async with stack.owner.begin() as connection:
        sso = (
            await connection.execute(
                text(
                    "INSERT INTO users (org_id, issuer, subject, display_name, role) "
                    "VALUES (:o, 'https://idp.example.org/', 'abc', 'Someone', 'member') "
                    "RETURNING id"
                ),
                {"o": stack.org_a},
            )
        ).scalar_one()
    slug = await slug_of(stack)
    with pytest.raises(RetentionError, match="identity provider"):
        await retention.delete_person(database.owner_url, org_slug=slug, user_id=sso, confirm=True)
    assert await one(stack, "SELECT count(*) FROM users WHERE id = :i", i=sso) == 1
    # By username it is not found at all: usernames are local accounts only.
    with pytest.raises(RetentionError, match="no such person"):
        await retention.delete_person(
            database.owner_url, org_slug=slug, username="abc", confirm=True
        )
    result = await retention.delete_person(
        database.owner_url, org_slug=slug, user_id=sso, confirm=True, allow_reprovision=True
    )
    assert result.deleted and result.username is None
    assert await one(stack, "SELECT count(*) FROM users WHERE id = :i", i=sso) == 0


async def test_the_same_username_in_two_organizations_is_deleted_in_one_only(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    await person(stack, "admin")
    async with stack.owner.begin() as connection:
        # The other organization's first: a lookup without the organization would find it.
        for org in (stack.org_b, stack.org_a):
            await connection.execute(
                text(
                    "INSERT INTO users (org_id, issuer, subject, display_name, role) "
                    "VALUES (:o, 'local', 'twin', 'Twin', 'member')"
                ),
                {"o": org},
            )
    await retention.delete_person(
        database.owner_url, org_slug=await slug_of(stack), username="twin", confirm=True
    )
    assert await one(stack, "SELECT count(*) FROM users WHERE subject = 'twin'") == 1
    assert await one(stack, "SELECT org_id FROM users WHERE subject = 'twin'") == stack.org_b


async def test_two_administrators_deleted_at_the_same_time_leave_one(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    first = await person(stack, "admin")
    second = await person(stack, "admin")
    slug = await slug_of(stack)

    async def delete(who: uuid.UUID) -> object:
        return await retention.delete_person(
            database.owner_url, org_slug=slug, user_id=who, confirm=True
        )

    outcomes = await asyncio.gather(delete(first.id), delete(second.id), return_exceptions=True)
    refused = [o for o in outcomes if isinstance(o, RetentionError)]
    done = [o for o in outcomes if isinstance(o, retention.PersonDeleted)]
    assert len(refused) == 1 and len(done) == 1, outcomes
    assert (
        await one(
            stack,
            "SELECT count(*) FROM users WHERE id = ANY(:ids) AND role = 'admin'",
            ids=[first.id, second.id],
        )
        == 1
    )


async def test_the_application_role_cannot_delete_a_person_or_an_audit_entry(
    make_stack: MakeStack, app_engine: Any
) -> None:
    """Only the table owner can: the commands run as the owner, never as the application."""
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    async with app_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(stack.org_a)}
        )
        for statement in ("DELETE FROM users WHERE id = :i", "DELETE FROM audit_logs"):
            nested = await connection.begin_nested()
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(statement), {"i": who.id} if ":i" in statement else {}
                )
            await nested.rollback()


# --- the audit log ---------------------------------------------------------------------------------


async def audit_entry(
    stack: Stack, action: str, when: datetime, org: uuid.UUID | None = None
) -> None:
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO audit_logs (org_id, occurred_at, actor_type, action, outcome) "
                "VALUES (:o, :w, 'system', :a, 'success')"
            ),
            {"o": org or stack.org_a, "w": when, "a": action},
        )


async def test_the_audit_log_is_pruned_at_the_boundary_and_says_so(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    cutoff = NOW - timedelta(days=365)
    await audit_entry(stack, "test.before", cutoff - timedelta(seconds=1))
    await audit_entry(stack, "test.on_the_line", cutoff)
    await audit_entry(stack, "test.after", cutoff + timedelta(seconds=1))
    await audit_entry(stack, "test.other_org", cutoff - timedelta(days=100), org=stack.org_b)
    slug = await slug_of(stack)
    seen = await retention.prune_audit(database.owner_url, org_slug=slug, days=365, now=NOW)
    assert seen.deleted is False and seen.rows == 1  # a count, nothing deleted
    assert len(entries(await audit_rows(stack), "test.before")) == 1
    result = await retention.prune_audit(
        database.owner_url, org_slug=slug, days=365, confirm=True, now=NOW
    )
    assert result.deleted and result.rows == 1
    left = {e["action"] for e in await audit_rows(stack)}
    assert "test.before" not in left  # strictly older than the cutoff: gone
    assert {"test.on_the_line", "test.after"} <= left  # on the line and newer: kept
    assert len(await audit_rows(stack, stack.org_b)) == 1  # another organization: untouched
    pruned = entries(await audit_rows(stack), "audit.pruned")
    assert len(pruned) == 1 and details_of(pruned[0]) == {"rows": 1, "older_than_days": 365}
    # A second run finds nothing more and leaves a second entry, not a gap.
    again = await retention.prune_audit(
        database.owner_url, org_slug=slug, days=365, confirm=True, now=NOW
    )
    assert again.rows == 0
    assert len(entries(await audit_rows(stack), "audit.pruned")) == 2


@pytest.mark.parametrize("days", [0, -5, 29])
async def test_an_audit_retention_under_thirty_days_is_refused(
    make_stack: MakeStack, database: TestDatabase, days: int
) -> None:
    stack: Stack = await make_stack("on")
    with pytest.raises(RetentionError, match="at least 30 days"):
        await retention.prune_audit(
            database.owner_url, org_slug=await slug_of(stack), days=days, confirm=True
        )
    # The control: the floor itself is accepted.
    ok = await retention.prune_audit(
        database.owner_url, org_slug=await slug_of(stack), days=30, confirm=True
    )
    assert ok.deleted


# --- what people did with the cards ------------------------------------------------------------------


async def test_old_months_are_folded_into_counts_and_their_interactions_deleted(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    users = await members(stack, 6)
    item = await article(stack, stack.org_a, "x")
    cutoff = datetime(2025, 11, 1, tzinfo=UTC)
    october = datetime(2025, 10, 15, 9, tzinfo=UTC)
    for user in users:  # six people in October: at the floor, so the month is kept
        await interaction(stack, user, item, "useful", october)
    for user, minutes in zip(users, (100, 200, 25, 30, 30, 30), strict=True):
        await interaction(stack, user, item, "time_saved", october, value=minutes)
    for user in users[:3]:  # three people in September: below the floor, so not kept
        await interaction(stack, user, item, "save", datetime(2025, 9, 3, tzinfo=UTC))
    # The boundary: one second before the cutoff is folded, the cutoff itself is kept.
    await interaction(stack, users[0], item, "tried", cutoff - timedelta(seconds=1))
    await interaction(stack, users[0], item, "view", cutoff)
    await interaction(stack, users[1], item, "view", NOW - timedelta(days=2))
    slug = await slug_of(stack)

    seen = await retention.fold_interactions(database.owner_url, org_slug=slug, months=12, now=NOW)
    assert seen.deleted is False and seen.cutoff == cutoff.date()
    assert (seen.rows, seen.months_kept, seen.months_dropped) == (6 + 6 + 3 + 1, 2, 2)
    assert await one(stack, "SELECT count(*) FROM interactions") == 18  # nothing deleted yet
    assert await one(stack, "SELECT count(*) FROM interaction_counts_monthly") == 0

    done = await retention.fold_interactions(
        database.owner_url, org_slug=slug, months=12, confirm=True, now=NOW
    )
    assert done.deleted and done.rows == 16
    async with stack.owner.connect() as connection:
        monthly = {
            (str(r.month), r.type): (r.people, r.actions, r.minutes)
            for r in await connection.execute(
                text("SELECT month, type, people, actions, minutes FROM interaction_counts_monthly")
            )
        }
    # 100 + 120 (the cap of one person) + 25 + 30 + 30 + 30 = 335, rounded to a multiple of 30.
    assert monthly == {
        ("2025-10-01", "useful"): (6, 6, None),
        ("2025-10-01", "time_saved"): (6, 6, 330),
    }
    # Nothing for September (three people) or for the boundary second (one person): the privacy floor.
    assert not any(month == "2025-09-01" for month, _ in monthly)
    kept = await one(stack, "SELECT count(*) FROM interactions")
    assert kept == 2  # the one on the cutoff and the recent one
    assert (
        await one(stack, "SELECT count(*) FROM interactions WHERE created_at < :c", c=cutoff) == 0
    )
    # The audit entry holds counts and the date, nothing about anybody.
    folded = entries(await audit_rows(stack), "interactions.folded")
    assert len(folded) == 1
    assert details_of(folded[0]) == {
        "rows": 16,
        "months_kept": 2,
        "months_dropped": 2,
        "before": "2025-11-01",
    }
    # A second run finds nothing to do and does not change a count.
    again = await retention.fold_interactions(
        database.owner_url, org_slug=slug, months=12, confirm=True, now=NOW
    )
    assert (again.rows, again.months_kept) == (0, 0)
    assert await one(stack, "SELECT sum(people) FROM interaction_counts_monthly") == 12


async def test_another_organizations_interactions_are_not_folded(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    users = await members(stack, 6, org=stack.org_b)
    item = await article(stack, stack.org_b, "theirs")
    for user in users:
        await interaction(
            stack, user, item, "useful", datetime(2025, 5, 1, tzinfo=UTC), org=stack.org_b
        )
    result = await retention.fold_interactions(
        database.owner_url, org_slug=await slug_of(stack), months=12, confirm=True, now=NOW
    )
    assert result.rows == 0
    assert (
        await one(stack, "SELECT count(*) FROM interactions WHERE org_id = :o", o=stack.org_b) == 6
    )
    assert await one(stack, "SELECT count(*) FROM interaction_counts_monthly") == 0


@pytest.mark.parametrize("months", [0, -1, 2])
async def test_an_interactions_retention_under_three_months_is_refused(
    make_stack: MakeStack, database: TestDatabase, months: int
) -> None:
    stack: Stack = await make_stack("on")
    with pytest.raises(RetentionError, match="at least 3 months"):
        await retention.fold_interactions(
            database.owner_url, org_slug=await slug_of(stack), months=months, confirm=True
        )
    ok = await retention.fold_interactions(
        database.owner_url, org_slug=await slug_of(stack), months=3, confirm=True
    )
    assert ok.deleted


async def test_the_table_of_counts_refuses_a_group_below_the_floor_whoever_writes_it(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    for people in (4, 0):
        with pytest.raises(DBAPIError):
            async with stack.owner.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO interaction_counts_monthly (org_id, month, type, people, actions) "
                        "VALUES (:o, '2025-01-01', 'view', :p, 10)"
                    ),
                    {"o": stack.org_a, "p": people},
                )
    for month in ("2025-01-15", "2025-01-31"):  # not the first day
        with pytest.raises(DBAPIError):
            async with stack.owner.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO interaction_counts_monthly (org_id, month, type, people, actions) "
                        "VALUES (:o, :m, 'view', 5, 10)"
                    ),
                    {"o": stack.org_a, "m": datetime.fromisoformat(month).date()},
                )
    async with stack.owner.begin() as connection:  # the control: the floor itself is accepted
        await connection.execute(
            text(
                "INSERT INTO interaction_counts_monthly (org_id, month, type, people, actions) "
                "VALUES (:o, '2025-01-01', 'view', 5, 10)"
            ),
            {"o": stack.org_a},
        )


async def test_the_application_and_the_worker_roles_have_no_access_to_the_monthly_counts(
    make_stack: MakeStack, app_engine: Any
) -> None:
    stack: Stack = await make_stack("on")
    async with app_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(stack.org_a)}
        )
        for statement in (
            "SELECT * FROM interaction_counts_monthly",
            "INSERT INTO interaction_counts_monthly (org_id, month, type, people, actions) "
            "VALUES (pharos_current_org(), '2025-01-01', 'view', 5, 5)",
            "DELETE FROM interaction_counts_monthly",
        ):
            nested = await connection.begin_nested()
            with pytest.raises(DBAPIError):
                await connection.execute(text(statement))
            await nested.rollback()


async def test_one_persons_minutes_are_capped_as_a_person_not_as_a_row(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    users = await members(stack, 5)
    october = datetime(2025, 10, 15, 9, tzinfo=UTC)
    for number, user in enumerate(users):
        for index in range(3 if number == 0 else 1):  # the first person logs three times
            item = await article(stack, stack.org_a, f"a-{number}-{index}")
            await interaction(
                stack, user, item, "time_saved", october, value=100 if number == 0 else 30
            )
    await retention.fold_interactions(
        database.owner_url, org_slug=await slug_of(stack), months=12, confirm=True, now=NOW
    )
    row = await one(
        stack,
        "SELECT minutes FROM interaction_counts_monthly WHERE type = 'time_saved'",
    )
    # 3 x 100 would be 300 for the first person; the cap is 120 for a person: 120 + 4 x 30 = 240.
    assert row == 240


async def test_a_month_that_already_has_a_count_stops_the_fold_and_deletes_nothing(
    make_stack: MakeStack, database: TestDatabase
) -> None:
    stack: Stack = await make_stack("on")
    users = await members(stack, 5)
    item = await article(stack, stack.org_a, "x")
    october = datetime(2025, 10, 15, 9, tzinfo=UTC)
    for user in users:
        await interaction(stack, user, item, "useful", october)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO interaction_counts_monthly (org_id, month, type, people, actions) "
                "VALUES (:o, '2025-10-01', 'useful', 5, 5)"
            ),
            {"o": stack.org_a},
        )
    with pytest.raises(RetentionError, match="already exists"):
        await retention.fold_interactions(
            database.owner_url, org_slug=await slug_of(stack), months=12, confirm=True, now=NOW
        )
    assert await one(stack, "SELECT count(*) FROM interactions") == 5
    assert entries(await audit_rows(stack), "interactions.folded") == []


async def test_the_worker_role_cannot_touch_the_counts_the_people_or_the_log(
    make_stack: MakeStack, worker_engine: Any
) -> None:
    stack: Stack = await make_stack("on")
    who = await person(stack, "member")
    async with worker_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(stack.org_a)}
        )
        for statement in (
            "SELECT * FROM interaction_counts_monthly",
            "DELETE FROM users WHERE id = :i",
            "DELETE FROM audit_logs",
            "DELETE FROM interactions",
        ):
            nested = await connection.begin_nested()
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(statement), {"i": who.id} if ":i" in statement else {}
                )
            await nested.rollback()
