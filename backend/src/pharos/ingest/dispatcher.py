"""Deciding which sources to fetch now, and handing each to a worker exactly once at a time.

A beat entry runs `dispatch` every minute. It takes a lease on the sources that are due and then
(only after the lease is committed) enqueues one task per source carrying two identifiers: the
source and the lease token. Nothing else travels through the broker, so whoever can write to it
cannot choose a URL (ADR-0012).

A lease says "this fetch is running" and ends on its own. If a worker dies, the lease runs out and
the source is due again; if a task is delivered twice, the second one finds the lease gone or
changed and does nothing. At most `per_tick` sources are started per run and at most
`in_flight_cap` are leased at any time, so a flood of due sources can never fill the broker.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.ingest import repository

logger = logging.getLogger(__name__)

PER_TICK = 20
IN_FLIGHT_CAP = 40
# Longer than a task may run (the hard time limit), so a live fetch never loses its lease.
LEASE_SECONDS = 300

Enqueue = Callable[[uuid.UUID, uuid.UUID], None]


@dataclass(frozen=True, slots=True)
class DispatchReport:
    leased: int
    enqueued: int
    failed_to_enqueue: int


async def dispatch(
    engine: AsyncEngine,
    *,
    slug: str,
    now: datetime,
    enqueue: Enqueue,
    per_tick: int = PER_TICK,
    in_flight_cap: int = IN_FLIGHT_CAP,
    lease_seconds: int = LEASE_SECONDS,
    personal: bool = True,
) -> DispatchReport:
    async with engine.begin() as connection:
        org = await repository.resolve_org(connection, slug)
        if org is None:
            logger.info("dispatch: the organization of this installation does not exist yet")
            return DispatchReport(0, 0, 0)
        await repository.enter_org(connection, org)
        room = in_flight_cap - await repository.count_in_flight(connection, now)
        leased = await repository.lease_due_sources(
            connection,
            now,
            now + timedelta(seconds=lease_seconds),
            min(per_tick, room),
            personal=personal,
        )
    # The lease is committed: a worker that receives the task will find it.
    enqueued = 0
    unlucky: list[repository.Leased] = []
    for item in leased:
        try:
            enqueue(item.id, item.lease_token)
            enqueued += 1
        except Exception as error:  # the broker is down or full; free the source for the next run
            logger.warning(
                "dispatch: could not enqueue a fetch", extra={"error": type(error).__name__}
            )
            unlucky.append(item)
    if unlucky:
        async with engine.begin() as connection:
            await repository.enter_org(connection, org)
            for item in unlucky:
                await repository.release_lease(connection, item.id, item.lease_token)
    return DispatchReport(len(leased), enqueued, len(unlucky))
