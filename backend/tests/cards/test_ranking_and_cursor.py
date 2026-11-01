"""The ranking tables and the cursor: what they must cover and how a cursor is refused."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from pharos.cards import cursor
from pharos.cards.ranking import (
    CURSOR_LIFETIME_SECONDS,
    INTEREST_CATEGORIES,
    LEVEL_CATEGORIES,
    WEIGHTS,
    categories_for_interests,
    categories_for_level,
)
from pharos.llm.schemas import CATEGORIES
from pharos.profile.catalog import AI_LEVELS, INTEREST_CODES

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
ITEM = uuid.UUID("40000000-0000-4000-8000-000000000001")


def test_the_weights_add_up_to_one_and_name_the_five_terms() -> None:
    assert round(sum(WEIGHTS.values()), 9) == 1.0
    assert set(WEIGHTS) == {"relevance", "recency", "quality", "interest", "level"}
    assert WEIGHTS["relevance"] > WEIGHTS["recency"] > WEIGHTS["quality"]  # the stated priority


def test_every_interest_and_level_of_a_profile_has_categories_and_they_exist() -> None:
    allowed = set(CATEGORIES.__args__)  # type: ignore[attr-defined]
    assert set(INTEREST_CATEGORIES) == set(INTEREST_CODES)
    assert set(LEVEL_CATEGORIES) == set(AI_LEVELS)
    for table in (*INTEREST_CATEGORIES.values(), *LEVEL_CATEGORIES.values()):
        assert table and set(table) <= allowed


def test_categories_for_a_person_are_sorted_unique_and_empty_when_nothing_is_known() -> None:
    assert categories_for_interests(["learning", "writing"]) == sorted(
        {"tutorial", "research", "news", "tool", "technique"}
    )
    assert categories_for_interests([]) == []
    assert categories_for_interests(["not_an_interest"]) == []
    assert categories_for_level("beginner") == ["news", "tool", "tutorial"]
    assert categories_for_level(None) == [] and categories_for_level("expert") == []


def test_a_cursor_round_trips() -> None:
    raw = cursor.encode(NOW, 123456, ITEM)
    parsed = cursor.decode(raw, now=NOW + timedelta(minutes=5))
    assert parsed is not None and (parsed.a, parsed.s, parsed.i) == (NOW, 123456, ITEM)
    assert "=" not in raw and "/" not in raw and "+" not in raw  # safe in a URL


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "x",
        "!!!!",
        "e30",  # {}
        "W10",  # []
        "a" * 201,
        # extra field
        cursor.encode(NOW, 1, ITEM) + "AA",
    ],
)
def test_a_malformed_cursor_is_refused(raw: str) -> None:
    assert cursor.decode(raw, now=NOW) is None


@pytest.mark.parametrize("age", [-1, CURSOR_LIFETIME_SECONDS + 1, 10**7])
def test_a_cursor_from_the_future_or_older_than_an_hour_is_refused(age: int) -> None:
    raw = cursor.encode(NOW, 1, ITEM)
    assert cursor.decode(raw, now=NOW + timedelta(seconds=age)) is None


def test_the_edges_of_the_lifetime_are_accepted() -> None:
    raw = cursor.encode(NOW, 1, ITEM)
    assert cursor.decode(raw, now=NOW) is not None
    assert cursor.decode(raw, now=NOW + timedelta(seconds=CURSOR_LIFETIME_SECONDS)) is not None


def test_a_negative_or_non_integer_score_is_refused() -> None:
    import base64
    import json

    def make(body: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")

    when = NOW.isoformat()
    assert cursor.decode(make({"a": when, "s": -1, "i": str(ITEM)}), now=NOW) is None
    assert cursor.decode(make({"a": when, "s": 1.5, "i": str(ITEM)}), now=NOW) is None
    assert cursor.decode(make({"a": when, "s": 2**63, "i": str(ITEM)}), now=NOW) is None  # no 500
    assert cursor.decode(make({"a": when, "s": "1", "i": str(ITEM)}), now=NOW) is None
    assert (
        cursor.decode(make({"a": "2026-11-01T12:00:00", "s": 1, "i": str(ITEM)}), now=NOW) is None
    )
    assert cursor.decode(make({"a": when, "s": 1, "i": "not-a-uuid"}), now=NOW) is None
    assert cursor.decode(make({"a": when, "s": 1, "i": str(ITEM), "x": 1}), now=NOW) is None
