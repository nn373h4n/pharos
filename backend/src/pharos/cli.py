"""Operator commands. Run inside the stack:

    docker compose run --rm migrate python -m pharos.cli create-org --slug acme --name "Acme"
    docker compose exec api python -m pharos.cli create-admin --username admin
    docker compose exec api python -m pharos.cli set-password --username admin
    docker compose exec api python -m pharos.cli unlock --username admin
    docker compose exec api python -m pharos.cli import-sources [--file PATH] [--dry-run]

`create-org` needs the table owner's credentials (the application role may not create
organizations), so it runs in the `migrate` service. The others run as the application role inside a
tenant transaction, so row-level security applies to them exactly as to the API.

Passwords are never taken from the command line (it would end up in the process list and the shell
history): they are read with a hidden prompt, or from standard input with `--password-stdin`.
"""

import argparse
import asyncio
import getpass
import re
import sys
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from pharos.core.audit import ActorType, AuditAction, AuditLog, Outcome
from pharos.core.clock import SystemClock
from pharos.core.config import MigrationSettings, Settings, get_settings
from pharos.core.db import create_engine, create_session_factory
from pharos.core.organization import OrganizationNotConfigured, OrganizationResolver
from pharos.core.ratelimit import LoginThrottle, RateLimiter
from pharos.core.redis import url_for_database
from pharos.core.rls import tenant_session
from pharos.core.secrets import KeyPurpose, derive_key
from pharos.core.sessions import SessionStore
from pharos.identity import repository
from pharos.identity.passwords import PasswordHasher, PasswordPolicyError, check_new_password
from pharos.sources import catalogue
from pharos.sources.service import MAX_ORG_SOURCES

_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class CliError(Exception):
    """A problem the operator can fix. Printed without a traceback."""


def read_password(*, from_stdin: bool, username: str) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            # getpass would fall back to reading with the characters echoed.
            raise CliError("no terminal to ask for a password; use --password-stdin")
        password = getpass.getpass("New password: ")
        if getpass.getpass("Repeat the password: ") != password:
            raise CliError("the two passwords are not the same")
    try:
        check_new_password(password, username=username)
    except PasswordPolicyError as error:
        raise CliError(str(error)) from error
    return password


def _username(raw: str) -> str:
    username = repository.normalize_username(raw)
    if not repository.is_valid_username(username):
        raise CliError(
            "the username may contain lower-case letters, digits and . _ @ + - "
            "(1 to 128 characters, starting with a letter or digit)"
        )
    return username


async def create_organization(owner_url: str, *, slug: str, name: str) -> uuid.UUID:
    if not _SLUG.fullmatch(slug):
        raise CliError("the slug may contain lower-case letters, digits and hyphens only")
    if not 1 <= len(name.strip()) <= 200:
        raise CliError("the name needs 1 to 200 characters")
    engine = create_async_engine(owner_url)
    try:
        async with engine.begin() as connection:
            existing = (
                await connection.execute(
                    text("SELECT 1 FROM organizations WHERE slug = :slug"), {"slug": slug}
                )
            ).first()
            if existing:
                raise CliError(f"an organization with the slug {slug!r} already exists")
            org_id: uuid.UUID = (
                await connection.execute(
                    text(
                        "INSERT INTO organizations (name, slug) VALUES (:name, :slug) RETURNING id"
                    ),
                    {"name": name.strip(), "slug": slug},
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO audit_logs (org_id, actor_type, action, target, outcome) "
                    "VALUES (:org, 'operator_cli', :action, :target, 'success')"
                ),
                {"org": org_id, "action": AuditAction.ORGANIZATION_CREATED.value, "target": slug},
            )
    finally:
        await engine.dispose()
    return org_id


class _Context:
    """The application-role connection and the pieces a command needs."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_engine(settings.database_url.get_secret_value())
        self.factory = create_session_factory(self.engine)
        self.audit = AuditLog(self.factory, derive_key(settings.secret_key, KeyPurpose.AUDIT_IP))

    async def org_id(self) -> uuid.UUID:
        try:
            return await OrganizationResolver(self.engine, self.settings.org_slug).resolve()
        except OrganizationNotConfigured:
            raise CliError(
                f"the organization {self.settings.org_slug!r} does not exist; run create-org first"
            ) from None

    async def close(self) -> None:
        await self.engine.dispose()


@asynccontextmanager
async def _redis(settings: Settings) -> AsyncIterator[Redis]:
    redis = Redis.from_url(
        url_for_database(settings.redis_url.get_secret_value(), settings.session_redis_db),
        socket_timeout=2,
        socket_connect_timeout=2,
    )
    try:
        yield redis
    finally:
        await redis.aclose()


def _hasher(settings: Settings) -> PasswordHasher:
    return PasswordHasher(
        memory_kib=settings.password_hash_memory_kib, time_cost=settings.password_hash_time_cost
    )


async def create_admin(
    settings: Settings,
    *,
    username: str,
    password: str,
    display_name: str | None = None,
    email: str | None = None,
) -> uuid.UUID:
    username = _username(username)
    context = _Context(settings)
    try:
        org_id = await context.org_id()
        password_hash = await _hasher(settings).hash(password)
        async with tenant_session(context.factory, org_id) as session:
            if await repository.find_user_id_by_username(session, org_id, username):
                raise CliError(f"the user {username!r} already exists")
            user_id = await repository.create_local_user(
                session,
                org_id=org_id,
                username=username,
                display_name=(display_name or username)[:200],
                email=email,
                role="admin",
                password_hash=password_hash,
            )
            await context.audit.record(
                session,
                org_id=org_id,
                action=AuditAction.USER_CREATED,
                actor_type=ActorType.OPERATOR_CLI,
                target=f"user:{user_id}",
                details={"method": "local", "role": "admin"},
            )
    finally:
        await context.close()
    return user_id


async def set_password(settings: Settings, *, username: str, password: str) -> None:
    username = _username(username)
    context = _Context(settings)
    try:
        org_id = await context.org_id()
        password_hash = await _hasher(settings).hash(password)
        async with tenant_session(context.factory, org_id) as session:
            user_id = await repository.find_user_id_by_username(session, org_id, username)
            if user_id is None or not await repository.set_password_hash(
                session, org_id, user_id, password_hash
            ):
                raise CliError(f"there is no local account named {username!r}")
            await context.audit.record(
                session,
                org_id=org_id,
                action=AuditAction.PASSWORD_SET,
                actor_type=ActorType.OPERATOR_CLI,
                target=f"user:{user_id}",
            )
        # A new password is usually set because the old one may be known to someone else, so every
        # session of the account ends and its sign-in delays are forgotten. After the commit, so a
        # failed change does not sign anyone out.
        async with _redis(settings) as redis:
            await SessionStore(
                redis,
                SystemClock(),
                idle_seconds=settings.session_idle_seconds,
                absolute_seconds=settings.session_absolute_seconds,
            ).revoke_user(user_id)
            await LoginThrottle(RateLimiter(redis, SystemClock())).unlock(username)
    finally:
        await context.close()


async def unlock(settings: Settings, *, username: str) -> int:
    username = _username(username)
    context = _Context(settings)
    try:
        org_id = await context.org_id()
        async with _redis(settings) as redis:
            removed = await LoginThrottle(RateLimiter(redis, SystemClock())).unlock(username)
        await context.audit.record_independently(
            org_id=org_id,
            action=AuditAction.LOGIN_UNLOCKED,
            outcome=Outcome.SUCCESS,
            actor_type=ActorType.OPERATOR_CLI,
            target=f"local:{username}",
        )
    finally:
        await context.close()
    return removed


async def import_sources(
    settings: Settings, *, file: Path | None, dry_run: bool
) -> catalogue.Result:
    """Add the sources of the starter catalogue (or of `file`) to the organization, switched off."""
    try:
        own_host = (urlsplit(settings.public_url).hostname or "").lower()
        entries = catalogue.load(file, blocked_hosts=(own_host,) if own_host else ())
    except catalogue.CatalogueError as error:
        raise CliError(str(error)) from error
    context = _Context(settings)
    try:
        org_id = await context.org_id()
        try:
            async with tenant_session(context.factory, org_id) as session:
                return await catalogue.import_entries(
                    session,
                    context.audit,
                    org_id,
                    entries,
                    dry_run=dry_run,
                    max_sources=MAX_ORG_SOURCES,
                )
        except catalogue.CatalogueError as error:
            raise CliError(str(error)) from error
    finally:
        await context.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pharos.cli", description=__doc__.split("\n")[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)

    org = commands.add_parser("create-org", help="create the organization of this installation")
    org.add_argument("--slug", required=True)
    org.add_argument("--name", required=True)

    admin = commands.add_parser("create-admin", help="create a local administrator")
    admin.add_argument("--username", required=True)
    admin.add_argument("--display-name")
    admin.add_argument("--email")
    admin.add_argument("--password-stdin", action="store_true", help="read the password from stdin")

    password = commands.add_parser("set-password", help="set a new password for a local account")
    password.add_argument("--username", required=True)
    password.add_argument("--password-stdin", action="store_true")

    unlock_parser = commands.add_parser("unlock", help="clear sign-in delays for an account")
    unlock_parser.add_argument("--username", required=True)

    sources = commands.add_parser(
        "import-sources",
        help="add the starter catalogue of sources to the organization, switched off",
    )
    sources.add_argument("--file", type=Path, help="a catalogue file (default: the one shipped)")
    sources.add_argument("--dry-run", action="store_true", help="show what would be added")
    return parser


async def run(args: argparse.Namespace) -> str:
    if args.command == "create-org":
        owner_url = MigrationSettings().migrations_database_url.get_secret_value()  # type: ignore[call-arg]
        org_id = await create_organization(owner_url, slug=args.slug, name=args.name)
        return f"organization {args.slug!r} created ({org_id})"
    settings = get_settings()
    if args.command == "create-admin":
        password = read_password(from_stdin=args.password_stdin, username=args.username)
        user_id = await create_admin(
            settings,
            username=args.username,
            password=password,
            display_name=args.display_name,
            email=args.email,
        )
        return f"administrator {_username(args.username)!r} created ({user_id})"
    if args.command == "set-password":
        password = read_password(from_stdin=args.password_stdin, username=args.username)
        await set_password(settings, username=args.username, password=password)
        return f"password of {_username(args.username)!r} changed"
    if args.command == "unlock":
        removed = await unlock(settings, username=args.username)
        return f"cleared {removed} delay record(s) for {_username(args.username)!r}"
    if args.command == "import-sources":
        result = await import_sources(settings, file=args.file, dry_run=args.dry_run)
        verb = "would add" if args.dry_run else "added"
        return (
            f"{verb} {len(result.added)} source(s), switched off; "
            f"{len(result.existing)} already there"
        )
    raise CliError(f"unknown command {args.command!r}")  # pragma: no cover - argparse prevents it


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        print(asyncio.run(run(args)))
    except CliError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
