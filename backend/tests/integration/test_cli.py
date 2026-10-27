"""The operator commands against real PostgreSQL and Redis."""

import io
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pharos import cli
from pharos.core.clock import SystemClock
from pharos.core.config import Settings, get_settings
from pharos.core.errors import ApiError
from pharos.core.ratelimit import LoginThrottle, RateLimiter
from pharos.core.redis import url_for_database

from ..constants import FAKE_SECRET_KEY
from .conftest import TestDatabase

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


@pytest.fixture
async def org_slug(owner_engine: AsyncEngine) -> AsyncIterator[str]:
    slug = f"cli-{uuid.uuid4().hex[:8]}"
    yield slug
    async with owner_engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM audit_logs WHERE org_id IN (SELECT id FROM organizations WHERE slug = :s)"
            ),
            {"s": slug},
        )
        await connection.execute(text("DELETE FROM organizations WHERE slug = :s"), {"s": slug})


def settings_for(database: TestDatabase, redis_url: str, slug: str) -> Settings:
    return Settings(
        environment="test",
        database_url=database.app_url,  # type: ignore[arg-type]
        redis_url=redis_url,  # type: ignore[arg-type]
        secret_key=FAKE_SECRET_KEY,  # type: ignore[arg-type]
        org_slug=slug,
        password_hash_memory_kib=64,
        password_hash_time_cost=1,
    )


async def audit_actions(owner: AsyncEngine, slug: str) -> list[tuple[str, str]]:
    async with owner.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT actor_type, action FROM audit_logs WHERE org_id = "
                "(SELECT id FROM organizations WHERE slug = :s) ORDER BY id"
            ),
            {"s": slug},
        )
        return [(r.actor_type, r.action) for r in rows]


# --- create-org --------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_org_makes_the_organization_and_audits_it(
    database: TestDatabase, owner_engine: AsyncEngine, org_slug: str
) -> None:
    org_id = await cli.create_organization(database.owner_url, slug=org_slug, name="  Acme Ltd ")
    async with owner_engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT name, slug FROM organizations WHERE id = :id"), {"id": org_id}
            )
        ).one()
    assert (row.name, row.slug) == ("Acme Ltd", org_slug)
    assert await audit_actions(owner_engine, org_slug) == [("operator_cli", "org.created")]


@pytest.mark.anyio
async def test_create_org_refuses_a_duplicate_and_a_bad_slug(
    database: TestDatabase, org_slug: str
) -> None:
    await cli.create_organization(database.owner_url, slug=org_slug, name="One")
    with pytest.raises(cli.CliError, match="already exists"):
        await cli.create_organization(database.owner_url, slug=org_slug, name="Two")
    for slug in ("Bad Slug", "-x", "x-", "", "a_b"):
        with pytest.raises(cli.CliError, match="slug"):
            await cli.create_organization(database.owner_url, slug=slug, name="Name")
    with pytest.raises(cli.CliError, match="name"):
        await cli.create_organization(database.owner_url, slug=f"{org_slug}-n", name="  ")


# --- create-admin and set-password -------------------------------------------------------------


@pytest.mark.anyio
async def test_create_admin_stores_an_argon2id_hash_and_audits_it(
    database: TestDatabase, redis_url: str, owner_engine: AsyncEngine, org_slug: str
) -> None:
    await cli.create_organization(database.owner_url, slug=org_slug, name="Acme")
    settings = settings_for(database, redis_url, org_slug)
    user_id = await cli.create_admin(
        settings, username=" Admin ", password=PASSWORD, email="admin@example.test"
    )
    async with owner_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT u.issuer, u.subject, u.role, u.status, u.email, u.display_name, "
                    "c.password_hash FROM users u JOIN local_credentials c ON c.user_id = u.id "
                    "WHERE u.id = :id"
                ),
                {"id": user_id},
            )
        ).one()
    assert (row.issuer, row.subject, row.role, row.status) == ("local", "admin", "admin", "active")
    assert row.email == "admin@example.test"
    assert row.display_name == "admin"
    assert row.password_hash.startswith("$argon2id$")
    assert PASSWORD not in row.password_hash
    assert ("operator_cli", "user.created") in await audit_actions(owner_engine, org_slug)


@pytest.mark.anyio
async def test_create_admin_refuses_bad_input(
    database: TestDatabase, redis_url: str, org_slug: str
) -> None:
    settings = settings_for(database, redis_url, org_slug)
    with pytest.raises(cli.CliError, match="does not exist; run create-org"):
        await cli.create_admin(settings, username="admin", password=PASSWORD)
    await cli.create_organization(database.owner_url, slug=org_slug, name="Acme")
    for name in ("", "has space", "-lead", "x" * 129):
        with pytest.raises(cli.CliError, match="username"):
            await cli.create_admin(settings, username=name, password=PASSWORD)
    await cli.create_admin(settings, username="admin", password=PASSWORD)
    with pytest.raises(cli.CliError, match="already exists"):
        await cli.create_admin(settings, username="ADMIN", password=PASSWORD)


@pytest.mark.anyio
async def test_set_password_replaces_the_hash_and_audits_it(
    database: TestDatabase, redis_url: str, owner_engine: AsyncEngine, org_slug: str
) -> None:
    await cli.create_organization(database.owner_url, slug=org_slug, name="Acme")
    settings = settings_for(database, redis_url, org_slug)
    await cli.create_admin(settings, username="admin", password=PASSWORD)

    async def stored() -> str:
        async with owner_engine.connect() as connection:
            return str(
                (
                    await connection.execute(
                        text(
                            "SELECT c.password_hash FROM local_credentials c JOIN users u "
                            "ON u.id = c.user_id WHERE u.subject = 'admin' AND u.org_id = "
                            "(SELECT id FROM organizations WHERE slug = :s)"
                        ),
                        {"s": org_slug},
                    )
                ).scalar_one()
            )

    before = await stored()
    await cli.set_password(settings, username="admin", password="a completely different one")
    assert await stored() != before
    assert ("operator_cli", "credential.password_set") in await audit_actions(
        owner_engine, org_slug
    )
    with pytest.raises(cli.CliError, match="no local account"):
        await cli.set_password(settings, username="nobody", password=PASSWORD)


# --- unlock ------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unlock_clears_the_delays_of_one_account(
    database: TestDatabase, redis_url: str, owner_engine: AsyncEngine, org_slug: str
) -> None:
    await cli.create_organization(database.owner_url, slug=org_slug, name="Acme")
    settings = settings_for(database, redis_url, org_slug)
    redis = Redis.from_url(url_for_database(redis_url, settings.session_redis_db))
    throttle = LoginThrottle(RateLimiter(redis, SystemClock()))
    try:
        locked, other = f"locked-{org_slug}", f"other-{org_slug}"
        for _ in range(6):
            for name in (locked, other):
                with suppress(ApiError):  # being slowed down is the point
                    await throttle.check(name, "198.51.100.9")
        assert await cli.unlock(settings, username=locked) >= 2
        await throttle.check(locked, "198.51.100.9")  # may try again
        with pytest.raises(ApiError):
            await throttle.check(other, "198.51.100.9")
    finally:
        await throttle.unlock(other)
        await redis.aclose()
    assert ("operator_cli", "auth.login_unlocked") in await audit_actions(owner_engine, org_slug)


# --- passwords and the command line ------------------------------------------------------------


def test_without_a_terminal_the_prompt_is_refused_instead_of_echoing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a terminal
    with pytest.raises(cli.CliError, match="--password-stdin"):
        cli.read_password(from_stdin=False, username="admin")


def test_the_password_is_read_from_standard_input_and_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert cli.read_password(from_stdin=True, username="admin") == PASSWORD
    monkeypatch.setattr("sys.stdin", io.StringIO("too short\n"))
    with pytest.raises(cli.CliError, match="characters"):
        cli.read_password(from_stdin=True, username="admin")


def test_the_interactive_prompt_asks_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: Iterator[str] = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr("sys.stdin", type("Tty", (io.StringIO,), {"isatty": lambda self: True})())
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert cli.read_password(from_stdin=False, username="admin") == PASSWORD
    answers = iter([PASSWORD, PASSWORD + "x"])
    with pytest.raises(cli.CliError, match="not the same"):
        cli.read_password(from_stdin=False, username="admin")


def test_the_command_line_has_no_password_option() -> None:
    parser = cli.build_parser()
    for command in ("create-admin", "set-password"):
        with pytest.raises(SystemExit):
            parser.parse_args([command, "--username", "a", "--password", "secret"])
    args = parser.parse_args(["create-admin", "--username", "a", "--password-stdin"])
    assert args.password_stdin is True


def test_main_runs_create_admin_end_to_end_without_leaking_the_password(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database: TestDatabase,
    redis_url: str,
) -> None:
    import asyncio

    slug = f"main-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PHAROS_ENVIRONMENT", "test")
    monkeypatch.setenv("PHAROS_DATABASE_URL", database.app_url)
    monkeypatch.setenv("PHAROS_REDIS_URL", redis_url)
    monkeypatch.setenv("PHAROS_MIGRATIONS_DATABASE_URL", database.owner_url)
    monkeypatch.setenv("PHAROS_SECRET_KEY", FAKE_SECRET_KEY)
    monkeypatch.setenv("PHAROS_ORG_SLUG", slug)
    monkeypatch.setenv("PHAROS_PASSWORD_HASH_MEMORY_KIB", "64")
    monkeypatch.setenv("PHAROS_PASSWORD_HASH_TIME_COST", "1")
    get_settings.cache_clear()
    try:
        assert cli.main(["create-org", "--slug", slug, "--name", "Main"]) == 0
        monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
        assert cli.main(["create-admin", "--username", "boss", "--password-stdin"]) == 0
        monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
        assert cli.main(["create-admin", "--username", "boss", "--password-stdin"]) == 1
    finally:
        get_settings.cache_clear()
        asyncio.run(_cleanup(database.owner_url, slug))
    captured = capsys.readouterr()
    assert PASSWORD not in captured.out + captured.err
    assert "error: the user 'boss' already exists" in captured.err
    assert "Traceback" not in captured.err


async def _cleanup(owner_url: str, slug: str) -> None:
    engine = create_async_engine(owner_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM audit_logs WHERE org_id IN (SELECT id FROM organizations WHERE slug = :s)"
            ),
            {"s": slug},
        )
        await connection.execute(text("DELETE FROM organizations WHERE slug = :s"), {"s": slug})
    await engine.dispose()
