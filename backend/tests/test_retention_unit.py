"""The month a fold of old interactions stops at (M5-09, ADR-0025), without a database."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from pharos.retention import month_cutoff

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "months", "expected"),
    [
        (NOW, 12, "2025-11-01"),
        (NOW, 1, "2026-10-01"),
        (NOW, 3, "2026-08-01"),
        (NOW, 120, "2016-11-01"),
        (datetime(2026, 1, 31, 23, 59, tzinfo=UTC), 1, "2025-12-01"),  # across a year
        (datetime(2026, 3, 1, tzinfo=UTC), 12, "2025-03-01"),  # the first of a month
        (datetime(2026, 2, 28, 23, 59, 59, tzinfo=UTC), 12, "2025-02-01"),  # the last second
    ],
)
def test_the_cutoff_is_the_first_day_of_the_month_that_many_months_back(
    now: datetime, months: int, expected: str
) -> None:
    assert month_cutoff(now, months).isoformat() == expected


def test_a_time_in_another_zone_is_read_in_utc() -> None:
    # 00:30 on 1 January in UTC+2 is still 31 December in UTC.
    local = datetime(2026, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    assert month_cutoff(local, 1).isoformat() == "2025-11-01"
    assert month_cutoff(local.astimezone(UTC), 1).isoformat() == "2025-11-01"


def test_a_row_is_kept_for_at_least_the_retention_and_at_most_a_month_more() -> None:
    """A row on the cutoff is kept; one second before it is folded: so the age at which a row goes
    is between `months` months and `months` months plus one."""
    cutoff = month_cutoff(NOW, 12)
    assert (NOW.date() - cutoff).days >= 365  # 2025-11-01 to 2026-11-05
    assert (NOW.date() - cutoff).days <= 365 + 31
