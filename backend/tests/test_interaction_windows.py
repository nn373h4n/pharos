"""Guard against a future change that lets the daily summary and the monthly counts overlap (F-47).

A day's count minus its month's count reveals a group smaller than the privacy floor of 5, so the
days the admin summary shows must all lie after the newest day that was folded into a month.

This is a guard against a change to the retention floor, `month_cutoff` or the summary window. It is
not proof that nothing leaks over time: an administrator who noted the daily counts earlier could
still subtract them from a monthly row that is shown to them (see F-47 and ADR-0025).
"""

from datetime import UTC, date, datetime, timedelta

from pharos.cards.service import SUMMARY_DAYS
from pharos.core.config import MIN_INTERACTIONS_RETENTION_MONTHS
from pharos.retention import month_cutoff

# From the configured floor up to the largest value the setting allows (core/config.py: le=120).
MONTHS = range(MIN_INTERACTIONS_RETENTION_MONTHS, 121)
# Five full years with two leap years (2024 and 2028), every day of them.
FIRST = date(2024, 1, 1)
LAST = date(2029, 1, 1)


def _days() -> list[date]:
    return [FIRST + timedelta(days=n) for n in range((LAST - FIRST).days + 1)]


def _gap(today: date, months: int) -> int:
    """Days between the newest folded day and `today`."""
    now = datetime(today.year, today.month, today.day, 12, tzinfo=UTC)
    newest_folded = month_cutoff(now, months) - timedelta(days=1)
    return (today - newest_folded).days


def test_the_sample_covers_leap_years() -> None:
    days = _days()
    assert len(days) > 5 * 365
    assert date(2024, 2, 29) in days
    assert date(2028, 2, 29) in days


def test_the_newest_folded_day_is_more_than_the_summary_window_back() -> None:
    """Guard: for every allowed retention, no day of the daily summary can be a folded day."""
    for today in _days():
        for months in MONTHS:
            gap = _gap(today, months)
            assert gap > SUMMARY_DAYS, (
                f"daily and monthly counts must not overlap (F-47): on {today} with a retention "
                f"of {months} months the newest folded day is {gap} days back, and the daily "
                f"summary looks back {SUMMARY_DAYS} days"
            )


def test_the_retention_floor_is_still_three_months() -> None:
    assert MIN_INTERACTIONS_RETENTION_MONTHS == 3, (
        "lowering MIN_INTERACTIONS_RETENTION_MONTHS moves folded months into the 30-day window of "
        "the daily summary: see F-47 and ADR-0025 before changing it"
    )


def test_the_smallest_gap_for_the_floor_is_ninety_days() -> None:
    smallest = min(_gap(today, MIN_INTERACTIONS_RETENTION_MONTHS) for today in _days())
    assert smallest == 90, (
        "daily and monthly counts must not overlap (F-47): the smallest gap for the floor was 90 "
        f"days and is now {smallest}; month_cutoff changed, so re-check the window of the daily "
        "summary"
    )
