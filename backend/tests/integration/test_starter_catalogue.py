"""`import-sources` against real PostgreSQL: the starter catalogue goes in switched off."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos import cli
from pharos.core.config import Settings
from pharos.sources.service import MAX_ORG_SOURCES

from ..constants import FAKE_SECRET_KEY
from .conftest import TestDatabase

pytestmark = pytest.mark.integration


@pytest.fixture
async def org_slug(owner_engine: AsyncEngine) -> AsyncIterator[str]:
    slug = f"cat-{uuid.uuid4().hex[:8]}"
    yield slug
    await forget(owner_engine, slug)


async def forget(owner: AsyncEngine, slug: str) -> None:
    """Remove the organization with what the tests put in it (sources and audit rows first)."""
    async with owner.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM sources WHERE org_id IN (SELECT id FROM organizations WHERE slug = :s)"
            ),
            {"s": slug},
        )
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
    )


def entry(n: int, **changes: Any) -> dict[str, Any]:
    return {
        "type": "rss",
        "name": f"Feed {n}",
        "config": {"url": f"https://host{n}.example.org/feed.xml"},
        "interval_seconds": 7200,
        "note": "A public feed for tests.",
    } | changes


def write(tmp_path: Path, *entries: dict[str, Any]) -> Path:
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps({"version": 1, "sources": list(entries)}))
    return path


async def sources_of(owner: AsyncEngine, slug: str) -> list[Any]:
    async with owner.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT name, type, enabled, interval_seconds, created_by, owner_user_id, "
                "config FROM sources WHERE org_id = (SELECT id FROM organizations WHERE slug = :s) "
                "ORDER BY name"
            ),
            {"s": slug},
        )
        return list(rows)


async def audit_rows(owner: AsyncEngine, slug: str) -> list[Any]:
    async with owner.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT actor_type, actor_id, action, target, outcome, details FROM audit_logs "
                "WHERE org_id = (SELECT id FROM organizations WHERE slug = :s) "
                "AND action = 'source.created' ORDER BY id"
            ),
            {"s": slug},
        )
        return list(rows)


@pytest.fixture
async def installation(
    database: TestDatabase, redis_url: str, org_slug: str
) -> tuple[Settings, str]:
    await cli.create_organization(database.owner_url, slug=org_slug, name="Acme")
    return settings_for(database, redis_url, org_slug), org_slug


@pytest.mark.anyio
async def test_every_source_is_added_switched_off_and_audited(
    installation: tuple[Settings, str], owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    settings, slug = installation
    github = {
        "type": "github",
        "name": "Widget releases",
        "config": {"repo": "acme/widget"},
        "interval_seconds": 21600,
        "note": "Releases of a public repository.",
    }
    file = write(tmp_path, entry(1), github)
    result = await cli.import_sources(settings, file=file, dry_run=False)
    assert (result.added, result.existing) == (["Feed 1", "Widget releases"], [])
    rows = await sources_of(owner_engine, slug)
    assert [row.name for row in rows] == ["Feed 1", "Widget releases"]
    assert all(row.enabled is False for row in rows)
    assert all(row.created_by is None and row.owner_user_id is None for row in rows)
    assert {row.name: row.interval_seconds for row in rows} == {
        "Feed 1": 7200,
        "Widget releases": 21600,
    }
    audit = await audit_rows(owner_engine, slug)
    assert len(audit) == 2
    for row in audit:
        assert (row.actor_type, row.actor_id, row.outcome) == ("operator_cli", None, "success")
        assert row.details["origin"] == "starter_catalogue"
        assert row.details["enabled"] is False
        assert row.target.startswith("source:")


@pytest.mark.anyio
async def test_a_second_run_adds_nothing(
    installation: tuple[Settings, str], owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    settings, slug = installation
    file = write(tmp_path, entry(1), entry(2))
    await cli.import_sources(settings, file=file, dry_run=False)
    again = await cli.import_sources(settings, file=file, dry_run=False)
    assert (again.added, again.existing) == ([], ["Feed 1", "Feed 2"])
    assert len(await sources_of(owner_engine, slug)) == 2
    assert len(await audit_rows(owner_engine, slug)) == 2


@pytest.mark.anyio
async def test_a_dry_run_writes_nothing_and_says_what_would_happen(
    installation: tuple[Settings, str], owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    settings, slug = installation
    first = write(tmp_path, entry(1))
    await cli.import_sources(settings, file=first, dry_run=False)
    both = write(tmp_path, entry(1), entry(2))
    result = await cli.import_sources(settings, file=both, dry_run=True)
    assert (result.added, result.existing) == (["Feed 2"], ["Feed 1"])
    assert [row.name for row in await sources_of(owner_engine, slug)] == ["Feed 1"]
    assert len(await audit_rows(owner_engine, slug)) == 1


@pytest.mark.anyio
async def test_what_the_administrator_changed_is_left_alone(
    installation: tuple[Settings, str], owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    settings, slug = installation
    file = write(tmp_path, entry(1))
    await cli.import_sources(settings, file=file, dry_run=False)
    async with owner_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sources SET enabled = true, name = 'Renamed', interval_seconds = 900 "
                "WHERE org_id = (SELECT id FROM organizations WHERE slug = :s)"
            ),
            {"s": slug},
        )
    result = await cli.import_sources(settings, file=file, dry_run=False)
    assert result.added == []
    (row,) = await sources_of(owner_engine, slug)
    assert (row.name, row.enabled, row.interval_seconds) == ("Renamed", True, 900)


@pytest.mark.anyio
async def test_the_cap_on_sources_applies_and_nothing_is_added_past_it(
    installation: tuple[Settings, str], owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    settings, slug = installation
    async with owner_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO sources (org_id, type, name, dedupe_key) "
                "SELECT o.id, 'rss', 'Old ' || n, 'old-' || n FROM organizations o, "
                "generate_series(1, :count) n WHERE o.slug = :s"
            ),
            {"s": slug, "count": MAX_ORG_SOURCES - 1},
        )
    file = write(tmp_path, entry(1), entry(2))
    with pytest.raises(cli.CliError, match="would pass"):
        await cli.import_sources(settings, file=file, dry_run=False)
    assert len(await sources_of(owner_engine, slug)) == MAX_ORG_SOURCES - 1
    assert await audit_rows(owner_engine, slug) == []
    one = write(tmp_path, entry(1))
    assert (await cli.import_sources(settings, file=one, dry_run=False)).added == ["Feed 1"]


@pytest.mark.anyio
async def test_a_dry_run_reports_the_cap_too(
    installation: tuple[Settings, str], owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    settings, slug = installation
    async with owner_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO sources (org_id, type, name, dedupe_key) "
                "SELECT o.id, 'rss', 'Old ' || n, 'old-' || n FROM organizations o, "
                "generate_series(1, :count) n WHERE o.slug = :s"
            ),
            {"s": slug, "count": MAX_ORG_SOURCES - 1},
        )
    file = write(tmp_path, entry(1), entry(2))
    with pytest.raises(cli.CliError, match="would pass"):
        await cli.import_sources(settings, file=file, dry_run=True)


@pytest.mark.anyio
async def test_the_host_of_this_installation_is_never_imported(
    installation: tuple[Settings, str], owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    settings, slug = installation
    own = settings.model_copy(update={"public_url": "https://pharos.example.org"})
    file = write(tmp_path, entry(1, config={"url": "https://pharos.example.org/feed.xml"}))
    with pytest.raises(cli.CliError, match="config refused"):
        await cli.import_sources(own, file=file, dry_run=False)
    assert await sources_of(owner_engine, slug) == []


@pytest.mark.anyio
async def test_two_imports_at_once_cannot_pass_the_cap_together(
    installation: tuple[Settings, str], owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    settings, slug = installation
    async with owner_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO sources (org_id, type, name, dedupe_key) "
                "SELECT o.id, 'rss', 'Old ' || n, 'old-' || n FROM organizations o, "
                "generate_series(1, :count) n WHERE o.slug = :s"
            ),
            {"s": slug, "count": MAX_ORG_SOURCES - 2},
        )
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    outcomes = await asyncio.gather(
        cli.import_sources(settings, file=write(first, entry(1), entry(2)), dry_run=False),
        cli.import_sources(settings, file=write(second, entry(3), entry(4)), dry_run=False),
        return_exceptions=True,
    )
    assert sum(isinstance(outcome, cli.CliError) for outcome in outcomes) == 1
    assert len(await sources_of(owner_engine, slug)) == MAX_ORG_SOURCES


@pytest.mark.anyio
async def test_a_broken_file_changes_nothing(
    installation: tuple[Settings, str], owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    settings, slug = installation
    file = write(tmp_path, entry(1), entry(2, config={"url": "https://localhost/feed.xml"}))
    with pytest.raises(cli.CliError, match="entry 2"):
        await cli.import_sources(settings, file=file, dry_run=False)
    assert await sources_of(owner_engine, slug) == []
    with pytest.raises(cli.CliError, match="cannot read"):
        await cli.import_sources(settings, file=tmp_path / "nope.json", dry_run=False)


@pytest.mark.anyio
async def test_the_catalogue_that_ships_imports_cleanly(
    installation: tuple[Settings, str], owner_engine: AsyncEngine
) -> None:
    settings, slug = installation
    result = await cli.import_sources(settings, file=None, dry_run=False)
    rows = await sources_of(owner_engine, slug)
    assert len(rows) == len(result.added)
    assert all(row.enabled is False for row in rows)


@pytest.mark.anyio
async def test_it_needs_the_organization_and_touches_no_other(
    database: TestDatabase,
    redis_url: str,
    org_slug: str,
    owner_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    file = write(tmp_path, entry(1))
    settings = settings_for(database, redis_url, org_slug)
    with pytest.raises(cli.CliError, match="run create-org first"):
        await cli.import_sources(settings, file=file, dry_run=False)
    other = f"{org_slug}-b"
    await cli.create_organization(database.owner_url, slug=other, name="Other")
    await cli.create_organization(database.owner_url, slug=org_slug, name="Acme")
    try:
        await cli.import_sources(settings, file=file, dry_run=False)
        assert len(await sources_of(owner_engine, org_slug)) == 1
        assert await sources_of(owner_engine, other) == []
    finally:
        await forget(owner_engine, other)


def test_the_command_has_no_way_to_switch_sources_on() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["import-sources", "--dry-run"])
    assert (args.command, args.dry_run, args.file) == ("import-sources", True, None)
    with pytest.raises(SystemExit):
        parser.parse_args(["import-sources", "--enabled"])
