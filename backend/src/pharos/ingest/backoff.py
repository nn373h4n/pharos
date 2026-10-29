"""When to fetch a source again: the normal rhythm, and the slower one after failures (ADR-0012).

Pure functions of the time, the source's interval and its failure count, so they are easy to test
and cannot surprise anybody. A failing source is not retried at the normal rhythm: the wait doubles
with every consecutive failure up to a day, a server that said "try again in N seconds" is obeyed,
and after `BREAKER_FAILURES` failures in a row the source is only tried once a day (the breaker is
open) until one fetch succeeds. A small, stable per-source spread keeps sources that were added
together from always arriving together.
"""

import uuid
from datetime import datetime, timedelta

MAX_BACKOFF = timedelta(hours=24)
BREAKER_FAILURES = 10
_SPREAD = 0.1


def _spread(source_id: uuid.UUID) -> float:
    """A fixed number in [0, 1) for this source."""
    return (source_id.int % 10_000) / 10_000


def after_success(now: datetime, interval_seconds: int, source_id: uuid.UUID) -> datetime:
    """The next regular fetch: the interval, give or take ten per cent, the same for each source."""
    factor = 1 + _SPREAD * (2 * _spread(source_id) - 1)
    return now + timedelta(seconds=interval_seconds * factor)


def after_failure(
    now: datetime,
    interval_seconds: int,
    failures: int,
    retry_after: int | None,
    source_id: uuid.UUID,
) -> datetime:
    """The next try after `failures` consecutive failures (at least 1), never sooner than asked."""
    if failures >= BREAKER_FAILURES:
        delay = MAX_BACKOFF
    else:
        delay = min(timedelta(seconds=interval_seconds) * 2 ** max(failures - 1, 0), MAX_BACKOFF)
    if retry_after is not None and retry_after > 0:
        delay = max(delay, min(timedelta(seconds=retry_after), MAX_BACKOFF))
    # Only ever later, so a Retry-After is never undercut.
    return now + delay * (1 + _SPREAD * _spread(source_id))


def breaker_is_open(failures: int) -> bool:
    return failures >= BREAKER_FAILURES
