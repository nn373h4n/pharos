"""Running one fetch of one source (ADR-0012).

The three phases keep the database out of the way of the network:

1. A short transaction checks that the lease is still the live one and reads the source.
2. The fetch itself runs with no transaction open: a slow or hostile host cannot hold a connection.
3. A short transaction writes the result, again only if the lease is still ours. If it is not (the
   lease ran out and another worker took over), the result is thrown away.

Everything is safe to repeat: a task delivered twice finds its lease gone, a rerun of the same feed
stores nothing new, and a run that died in the middle is marked abandoned by the cleanup.
"""

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.ingest import backoff, repository
from pharos.ingest.connectors.base import (
    ConfigError,
    Connector,
    ConnectorError,
    FetchOutcome,
    FetchState,
)
from pharos.ingest.hostgate import HostGate
from pharos.net.safe_http import SafeHttpClient

logger = logging.getLogger(__name__)

Result = Literal["ok", "not_modified", "failed", "stale", "deferred"]

# A fetch may run for the hard time limit of a task (150 s). Starting with less lease than that left
# could end with the lease running out in the middle, and the same host being fetched twice at once.
MIN_LEASE_LEFT = timedelta(seconds=160)


@dataclass(frozen=True, slots=True)
class FetchReport:
    result: Result
    items_new: int = 0
    error_code: str | None = None


class _LeaseLost(Exception):
    """Raised inside the writing transaction so that everything written so far is rolled back."""


async def fetch_source(
    engine: AsyncEngine,
    *,
    slug: str,
    source_id: uuid.UUID,
    lease_token: uuid.UUID,
    clock: Callable[[], datetime],
    connectors: Mapping[str, Connector],
    http: SafeHttpClient,
    gate: HostGate,
) -> FetchReport:
    # --- 1. Is this lease still the live one? -------------------------------------------------
    async with engine.begin() as connection:
        org = await repository.resolve_org(connection, slug)
        if org is None:
            return FetchReport("stale")
        await repository.enter_org(connection, org)
        source = await repository.load_leased(connection, source_id, lease_token, clock())
    if source is None:
        return FetchReport("stale")  # a duplicate delivery, an expired lease, a disabled source

    connector = connectors.get(source.type)
    config: dict[str, Any] | None = None
    failure: ConnectorError | None = None
    if connector is None:
        failure = ConnectorError("error", "unsupported")
    else:
        try:
            config = connector.validate_config(source.config)  # re-checked: the row is not trusted
        except ConfigError:
            failure = ConnectorError("error", "bad_config")

    # --- politeness: one request per host in a short time -------------------------------------
    if connector is not None and config is not None:
        host = connector.politeness_host(config)
        try:
            allowed = host is None or await gate.enter(host)
        except Exception:  # Redis is unreachable: be polite rather than risk a flood
            logger.warning("fetch: the host gate is unavailable; deferring")
            allowed = False
        if not allowed:
            async with engine.begin() as connection:
                await repository.enter_org(connection, org)
                later = clock() + timedelta(seconds=gate.gap_seconds * 2 + 1)
                await repository.defer(connection, source_id, lease_token, later)
            return FetchReport("deferred")

    # --- 2. Start the run, then fetch with no transaction open --------------------------------
    started = clock()
    async with engine.begin() as connection:
        await repository.enter_org(connection, org)
        began = await repository.start_run(
            connection, org, source_id, lease_token, started, MIN_LEASE_LEFT
        )
    if began is None:
        return FetchReport("stale")
    # The token was replaced: from here on only this task holds the lease, and a second copy of the
    # same task, which still has the old token, finds nothing.
    run, lease_token = began

    outcome: FetchOutcome | None = None
    if failure is None and connector is not None and config is not None:
        try:
            outcome = await connector.fetch(
                config, FetchState(source.etag, source.last_modified), http, started
            )
        except ConnectorError as error:
            failure = error
        except Exception as error:  # a bug in a connector must not take the worker down
            logger.error("fetch: unexpected error", extra={"error": type(error).__name__})
            failure = ConnectorError("error", "unexpected")

    # --- 3. Write the result, if the lease is still ours --------------------------------------
    try:
        async with engine.begin() as connection:
            await repository.enter_org(connection, org)
            if outcome is None:
                return await _write_failure(
                    connection, run, source, lease_token, failure, started, clock()
                )
            return await _write_success(
                connection, org, run, source, lease_token, outcome, started, clock()
            )
    except _LeaseLost:
        return FetchReport("stale")
    except Exception as error:
        # Saving failed (the database refused something in the feed, or went away). The
        # transaction was rolled back; without this the source would look untouched, be leased again
        # as soon as the lease ran out, and fail the same way for ever, ignoring every backoff.
        logger.error("fetch: could not store the result", extra={"error": type(error).__name__})
    try:
        async with engine.begin() as connection:
            await repository.enter_org(connection, org)
            return await _write_failure(
                connection,
                run,
                source,
                lease_token,
                ConnectorError("error", "store_failed"),
                started,
                clock(),
            )
    except _LeaseLost:
        return FetchReport("stale")


async def _write_failure(
    connection: Any,
    run: int,
    source: repository.SourceRow,
    token: uuid.UUID,
    failure: ConnectorError | None,
    started: datetime,
    finished: datetime,
) -> FetchReport:
    failure = failure or ConnectorError("error", "unexpected")
    failures = source.consecutive_failures + 1
    later = backoff.after_failure(
        finished, source.interval_seconds, failures, failure.retry_after, source.id
    )
    recorded = await repository.record_failure(
        connection, source.id, token, error_code=failure.code, next_fetch_at=later
    )
    if recorded is None:
        raise _LeaseLost
    await repository.finish_run(
        connection,
        run,
        outcome=failure.outcome,
        now=finished,
        started=started,
        error_code=failure.code,
        http_status=failure.http_status,
    )
    return FetchReport("failed", error_code=failure.code)


async def _write_success(
    connection: Any,
    org: uuid.UUID,
    run: int,
    source: repository.SourceRow,
    token: uuid.UUID,
    outcome: FetchOutcome,
    started: datetime,
    finished: datetime,
) -> FetchReport:
    new = 0
    if outcome.kind == "ok":
        new = await repository.store_raw_items(connection, org, source.id, outcome.entries)
    ours = await repository.record_success(
        connection,
        source.id,
        token,
        now=finished,
        next_fetch_at=backoff.after_success(finished, source.interval_seconds, source.id),
        etag=outcome.etag,
        last_modified=outcome.last_modified,
    )
    if not ours:
        raise _LeaseLost  # rolls back the raw items written above: another worker owns this source
    await repository.finish_run(
        connection,
        run,
        outcome="ok" if outcome.kind == "ok" else "not_modified",
        now=finished,
        started=started,
        bytes_fetched=outcome.bytes_fetched,
        items_seen=outcome.entries_seen,
        items_new=new,
    )
    return FetchReport(outcome.kind, items_new=new)
