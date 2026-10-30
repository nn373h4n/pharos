"""One trial fetch of a source, for an administrator who is about to enable it (ADR-0013).

It uses the connector and the `SafeHttpClient` of a scheduled fetch, so a good answer means the real
fetch will behave the same way. It differs in three ways: it holds no lease, it writes **nothing to
the database** (no run, no raw entry, no item, no validator, no failure count), and it leaves its
answer in Redis for a short while instead of in a table.

The task carries the identifiers of the source and of the test and nothing else. The address and the
configuration are read here, from the database, and only for an organization source: a person's own
feed is never fetched on an administrator's request, whatever the message says. No database
transaction is open while the network is used.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.ingest import repository, testfetch
from pharos.ingest.connectors.base import Connector, ConnectorError, FetchState
from pharos.ingest.hostgate import HostGate
from pharos.net.safe_http import SafeHttpClient

logger = logging.getLogger(__name__)


class ResultStore(Protocol):
    def set(self, name: str, value: str, *, ex: int) -> Awaitable[Any]: ...


async def _load(
    engine: AsyncEngine, slug: str, source_id: uuid.UUID
) -> tuple[str, dict[str, Any]] | None:
    """The type and the configuration of an organization source, or None. A short transaction of
    its own, closed before any network is used."""
    async with engine.begin() as connection:
        org = await repository.resolve_org(connection, slug)
        if org is None:
            return None
        await repository.enter_org(connection, org)
        row = (
            await connection.execute(
                text(
                    "SELECT type, config FROM sources "
                    "WHERE org_id = :org AND id = :id AND owner_user_id IS NULL"
                ),
                {"org": org, "id": source_id},
            )
        ).first()
    return None if row is None else (row.type, dict(row.config))


def _answer(
    test_id: uuid.UUID, source_id: uuid.UUID, outcome: str, **fields: Any
) -> testfetch.TestResult:
    return testfetch.TestResult.model_validate(
        {"v": 1, "test_id": test_id, "source_id": source_id, "outcome": outcome, **fields}
    )


async def run_test_fetch(
    engine: AsyncEngine,
    *,
    slug: str,
    source_id: uuid.UUID,
    test_id: uuid.UUID,
    connectors: Mapping[str, Connector],
    http: SafeHttpClient,
    gate: HostGate,
    store: ResultStore,
    clock: Callable[[], datetime],
) -> testfetch.TestResult:
    """Fetch once, store the answer, return it. Never raises for anything the source does."""
    loaded = await _load(engine, slug, source_id)
    if loaded is None:
        result = _answer(test_id, source_id, "source_missing")
    else:
        source_type, config = loaded
        result = await _fetch(
            source_type, config, test_id, source_id, connectors, http, gate, clock
        )
    await store.set(
        testfetch.result_key(test_id), testfetch.encode(result), ex=testfetch.RESULT_TTL_SECONDS
    )
    return result


async def _fetch(
    source_type: str,
    config: dict[str, Any],
    test_id: uuid.UUID,
    source_id: uuid.UUID,
    connectors: Mapping[str, Connector],
    http: SafeHttpClient,
    gate: HostGate,
    clock: Callable[[], datetime],
) -> testfetch.TestResult:
    connector = connectors.get(source_type)
    if connector is None:
        return _answer(test_id, source_id, "error", error_code="unsupported_type")
    try:
        checked = connector.validate_config(config)  # the stored configuration is never trusted
    except ValueError:
        return _answer(test_id, source_id, "error", error_code="config_invalid")
    host = connector.politeness_host(checked)
    if host is not None and not await gate.enter(host):
        return _answer(test_id, source_id, "host_busy")
    try:
        outcome = await connector.fetch(checked, FetchState(), http, clock())
    except ConnectorError as error:
        return _answer(
            test_id,
            source_id,
            error.outcome,
            error_code=error.code,
            http_status=error.http_status,
        )
    except Exception as error:  # a bug in a connector must not take the worker down
        logger.error("test fetch: unexpected error", extra={"error": type(error).__name__})
        return _answer(test_id, source_id, "error", error_code="unexpected")
    samples: list[testfetch.Sample] = []
    for found in outcome.entries:
        if len(samples) == testfetch.MAX_SAMPLES:
            break
        try:  # an article the reader would refuse is left out of the sample, not out of the answer
            samples.append(
                testfetch.Sample(title=found.title, url=found.url, published_at=found.published_at)
            )
        except ValueError:
            continue
    try:
        return _answer(
            test_id,
            source_id,
            "ok",
            entries_seen=outcome.entries_seen,
            entries_skipped=outcome.entries_skipped,
            bytes_fetched=outcome.bytes_fetched,
            sample=samples,
        )
    except ValueError:  # an answer the protocol itself would refuse is no answer: say so plainly
        return _answer(test_id, source_id, "error", error_code="unexpected")
