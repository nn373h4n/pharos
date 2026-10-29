"""When a source is tried again: the normal rhythm, backoff after failures, the breaker."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from pharos.ingest import backoff

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
SOURCE = uuid.UUID(int=5000)  # spread 0.5
HOUR = 3600


def minutes(delta: timedelta) -> float:
    return delta.total_seconds() / 60


def test_a_success_comes_back_after_the_interval_give_or_take_ten_per_cent() -> None:
    for n in range(0, 10_000, 137):
        later = backoff.after_success(NOW, HOUR, uuid.UUID(int=n))
        assert 0.9 * HOUR <= (later - NOW).total_seconds() <= 1.1 * HOUR


def test_the_spread_is_stable_for_a_source_and_differs_between_sources() -> None:
    first = backoff.after_success(NOW, HOUR, uuid.UUID(int=1))
    assert first == backoff.after_success(NOW, HOUR, uuid.UUID(int=1))
    assert (
        len({backoff.after_success(NOW, HOUR, uuid.UUID(int=n)) for n in range(0, 9000, 1000)}) > 5
    )


def test_the_middle_of_the_spread_is_exactly_the_interval() -> None:
    assert backoff.after_success(NOW, HOUR, uuid.UUID(int=5000)) == NOW + timedelta(seconds=HOUR)


@pytest.mark.parametrize(("failures", "waits"), [(1, 1), (2, 2), (3, 4), (4, 8), (5, 16)])
def test_the_wait_doubles_with_every_failure(failures: int, waits: int) -> None:
    zero_spread = uuid.UUID(int=0)
    later = backoff.after_failure(NOW, HOUR, failures, None, zero_spread)
    assert later - NOW == timedelta(hours=waits)


def test_the_wait_never_exceeds_a_day_and_the_spread_only_adds() -> None:
    zero, worst = uuid.UUID(int=0), uuid.UUID(int=9999)
    assert backoff.after_failure(NOW, HOUR, 8, None, zero) - NOW == backoff.MAX_BACKOFF
    assert backoff.after_failure(NOW, HOUR, 9, None, zero) - NOW == backoff.MAX_BACKOFF
    assert backoff.after_failure(NOW, HOUR, 9, None, worst) - NOW <= backoff.MAX_BACKOFF * 1.1
    for failures in range(1, 12):
        assert backoff.after_failure(NOW, 900, failures, None, worst) >= backoff.after_failure(
            NOW, 900, failures, None, zero
        )


def test_the_breaker_opens_at_ten_failures_in_a_row() -> None:
    zero = uuid.UUID(int=0)
    assert not backoff.breaker_is_open(backoff.BREAKER_FAILURES - 1)
    assert backoff.breaker_is_open(backoff.BREAKER_FAILURES)
    # Even a source fetched every five minutes is then tried once a day.
    assert backoff.after_failure(NOW, 300, backoff.BREAKER_FAILURES, None, zero) - NOW == (
        backoff.MAX_BACKOFF
    )
    assert backoff.after_failure(NOW, 300, 5, None, zero) - NOW < backoff.MAX_BACKOFF


@pytest.mark.parametrize("asked", [10, 600, 7200, 86_400, 10**9])
def test_a_server_that_asks_for_a_wait_is_obeyed_up_to_a_day(asked: int) -> None:
    later = backoff.after_failure(NOW, 300, 1, asked, SOURCE)
    assert later - NOW >= timedelta(seconds=min(asked, 86_400))
    assert later - NOW <= backoff.MAX_BACKOFF * 1.1


@pytest.mark.parametrize("asked", [None, 0, -5])
def test_no_or_nonsense_retry_after_is_ignored(asked: int | None) -> None:
    assert backoff.after_failure(NOW, 900, 1, asked, uuid.UUID(int=0)) - NOW == timedelta(
        seconds=900
    )
