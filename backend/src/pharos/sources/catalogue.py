"""The starter catalogue: public sources shipped with Pharos, imported by the operator.

`starter_sources.json` lists sources an installation may want. Importing writes them
**switched off**: a catalogue entry has no `enabled` field at all (an unknown field is refused), and
the importer never sets it. An administrator reads the list and switches on what is wanted.

Every entry is checked with the same rules as a source added through the API (type, name, interval,
and `pharos.ingest.sourceconfig`, which refuses addresses inside the network), so a catalogue that
is wrong fails when it is loaded, before anything is written. Nothing here fetches anything.
"""

import json
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from pharos.core.audit import ActorType, AuditAction, AuditLog
from pharos.ingest.sourceconfig import ConfigError, validate
from pharos.ingest.text import clean_text
from pharos.sources import repository
from pharos.sources.schemas import MAX_INTERVAL, MIN_INTERVAL, SourceType

CATALOGUE_VERSION = 1
MAX_ENTRIES = 100
MAX_FILE_BYTES = 512 * 1024
NAME_LIMIT = 200
NOTE_LIMIT = 300
DEFAULT_FILE = "starter_sources.json"


class CatalogueError(Exception):
    """The catalogue cannot be used. The text says where, for an operator to read."""


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: SourceType
    name: str = Field(min_length=1, max_length=NAME_LIMIT)
    config: dict[str, Any]
    interval_seconds: StrictInt = Field(ge=MIN_INTERVAL, le=MAX_INTERVAL)
    note: str = Field(min_length=1, max_length=NOTE_LIMIT)


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: StrictInt
    sources: list[dict[str, Any]] = Field(max_length=MAX_ENTRIES)


@dataclass(frozen=True, slots=True)
class Entry:
    type: str
    name: str
    config: dict[str, Any]
    dedupe_key: str
    interval_seconds: int
    note: str


@dataclass(frozen=True, slots=True)
class Result:
    added: list[str]
    existing: list[str]


def _plain(value: str, limit: int, where: str) -> str:
    stripped = value.strip()
    if not stripped or clean_text(stripped, limit) != stripped:
        raise CatalogueError(f"{where}: plain text only, no markup or control characters")
    return stripped


def parse(raw: bytes, *, blocked_hosts: tuple[str, ...] = ()) -> list[Entry]:
    """The checked entries of a catalogue file, or CatalogueError naming the first problem."""
    if len(raw) > MAX_FILE_BYTES:
        raise CatalogueError("the catalogue file is too large")
    try:
        document = _File.model_validate(json.loads(raw))
    except (ValueError, ValidationError, RecursionError) as error:
        raise CatalogueError(f"the catalogue file is not valid: {_first(error)}") from None
    if document.version != CATALOGUE_VERSION:
        raise CatalogueError(f"unsupported catalogue version {document.version}")
    entries: list[Entry] = []
    names: set[str] = set()
    keys: set[str] = set()
    for number, raw_entry in enumerate(document.sources, start=1):
        where = f"entry {number}"
        try:
            entry = _Entry.model_validate(raw_entry)
        except ValidationError as error:
            raise CatalogueError(f"{where}: {_first(error)}") from None
        name = _plain(entry.name, NAME_LIMIT, f"{where} name")
        note = _plain(entry.note, NOTE_LIMIT, f"{where} note")
        try:
            checked = validate(entry.type, entry.config, blocked_hosts=blocked_hosts)
        except ConfigError as error:
            raise CatalogueError(f"{where} ({name}): config refused: {error.code}") from None
        if entry.type == "rss" and not str(checked.config.get("url", "")).startswith("https://"):
            raise CatalogueError(f"{where} ({name}): a catalogue feed must use https")
        if name.casefold() in names:
            raise CatalogueError(f"{where}: the name {name!r} is used twice")
        if checked.dedupe_key in keys:
            raise CatalogueError(f"{where} ({name}): the same source is listed twice")
        names.add(name.casefold())
        keys.add(checked.dedupe_key)
        entries.append(
            Entry(
                entry.type, name, checked.config, checked.dedupe_key, entry.interval_seconds, note
            )
        )
    return entries


def _first(error: Exception) -> str:
    if isinstance(error, ValidationError):
        found = error.errors()[0]
        place = ".".join(ascii(str(part)) for part in found["loc"]) or "file"
        return f"{place}: {found['type']}"
    return "not JSON"


def load(path: Path | None = None, *, blocked_hosts: tuple[str, ...] = ()) -> list[Entry]:
    """The entries of `path`, or of the catalogue that ships with Pharos."""
    try:
        if path is None:
            raw = resources.files("pharos.sources").joinpath(DEFAULT_FILE).read_bytes()
        else:
            with path.open("rb") as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError as error:
        raise CatalogueError(f"cannot read the catalogue: {error.strerror or 'error'}") from None
    return parse(raw, blocked_hosts=blocked_hosts)


async def import_entries(
    session: AsyncSession,
    audit: AuditLog,
    org_id: uuid.UUID,
    entries: list[Entry],
    *,
    dry_run: bool,
    max_sources: int,
) -> Result:
    """Add what the organization does not have yet, switched off, all or nothing.

    A source with the same key is left alone and reported as existing; nothing already there is
    changed. The cap on the number of sources applies as it does for the API."""
    await repository.lock_org_sources(session, org_id)
    present = await repository.count_sources(session, org_id)
    added: list[str] = []
    existing: list[str] = []
    for entry in entries:
        if dry_run and await repository.exists_source(session, org_id, entry.dedupe_key):
            existing.append(entry.name)
            continue
        if present + len(added) >= max_sources:
            raise CatalogueError(
                f"the organization would pass {max_sources} sources; nothing added"
            )
        if dry_run:
            added.append(entry.name)
            continue
        created = await repository.insert_source(
            session,
            org_id,
            source_type=entry.type,
            name=entry.name,
            config=entry.config,
            dedupe_key=entry.dedupe_key,
            enabled=False,
            interval_seconds=entry.interval_seconds,
            created_by=None,
        )
        if created is None:
            existing.append(entry.name)
            continue
        added.append(entry.name)
        await audit.record(
            session,
            org_id=org_id,
            action=AuditAction.SOURCE_CREATED,
            actor_type=ActorType.OPERATOR_CLI,
            target=f"source:{created.id}",
            details={
                "type": entry.type,
                "interval_seconds": entry.interval_seconds,
                "enabled": False,
                "origin": "starter_catalogue",
            },
        )
    return Result(added, existing)
