"""How the cards are ordered. One formula, one table of weights, nothing learned.

score = relevance * 0.50 + recency * 0.20 + quality * 0.15 + interest * 0.10 + level * 0.05

* relevance: how well the article fits the person's profession, 0 to 1 (the model's score from
  `item_professions`; 0 when the model named no fit).
* recency: 0.5 to the power of (age in days / 7), so an article loses half its weight every week.
* quality: the model's score divided by 5.
* interest: 1 when the article's category suits one of the person's interests, else 0.
* level: 1 when the category suits the person's level, else 0.

The weights, the half-life and the two tables below are a first guess. They are in one place so
they can be tuned with real use and with the evaluation of M4-09; a test keeps the weights adding
up to one and the tables covering every interest and level the profile can hold.

The score is computed in the database at one moment (`as_of`) fixed by the first page, so the list
does not reorder itself while someone reads it. The first page is ranked at the start of the
current five minutes (`rank_moment`): everybody who asks in that time is ranked at the same
moment, which is what lets the shared part of the ranking be kept and reused (ADR-0027).
"""

from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Context, Decimal
from typing import Final

WEIGHTS: Final = {
    "relevance": 0.50,
    "recency": 0.20,
    "quality": 0.15,
    "interest": 0.10,
    "level": 0.05,
}
HALF_LIFE_DAYS: Final = 7.0
# Older articles are not candidates at all; their recency would be nearly nothing anyway.
WINDOW_DAYS: Final = 120
SCORE_SCALE: Final = 1_000_000  # the score is kept as a whole number so paging is exact

PAGE_SIZE: Final = 20
MAX_PAGE_SIZE: Final = 50
CURSOR_LIFETIME_SECONDS: Final = 3600

# Which categories of article suit which interest of the profile (a first guess).
INTEREST_CATEGORIES: Final = {
    "writing": ("tool", "tutorial", "technique"),
    "summarizing": ("tool", "technique", "tutorial"),
    "translation": ("tool", "tutorial"),
    "data_analysis": ("technique", "tool", "case_study", "research"),
    "reporting": ("technique", "case_study", "tool"),
    "research": ("research", "case_study"),
    "automation": ("tool", "technique", "case_study"),
    "coding": ("tool", "tutorial", "technique"),
    "meetings": ("tool", "tutorial"),
    "planning": ("technique", "case_study", "policy"),
    "customer_support": ("case_study", "tool", "tutorial"),
    "learning": ("tutorial", "research", "news"),
}

# Which categories suit a level: beginners want how-tos and tools, advanced readers the method.
LEVEL_CATEGORIES: Final = {
    "beginner": ("tutorial", "tool", "news"),
    "intermediate": ("technique", "case_study", "tool"),
    "advanced": ("research", "technique", "policy"),
}


def categories_for_interests(interests: list[str]) -> list[str]:
    found: set[str] = set()
    for interest in interests:
        found.update(INTEREST_CATEGORIES.get(interest, ()))
    return sorted(found)


def categories_for_level(level: str | None) -> list[str]:
    return sorted(LEVEL_CATEGORIES.get(level or "", ()))


# --- the person's part of the score, for the cached ranking (ADR-0027) ---

INTEREST_WEIGHT: Final = Decimal(str(WEIGHTS["interest"]))
LEVEL_WEIGHT: Final = Decimal(str(WEIGHTS["level"]))
# The most the person's own part can add to the shared part of an article's score.
MAX_BONUS: Final = INTEREST_WEIGHT + LEVEL_WEIGHT
# Five minutes: the ranking of a profession is made once in that time, and a request that arrives
# just as it is being made waits for it. New articles come hourly at the fastest and are read by
# the model a minute or more after that, so a lag of five minutes is not what people wait for.
RANK_MOMENT_SECONDS: Final = 300
_EXACT = Context(prec=80, rounding=ROUND_FLOOR)


def person_score(
    base: Decimal,
    category: str,
    interest_categories: frozenset[str],
    level_categories: frozenset[str],
) -> int:
    """The whole score of an article for a person, from the shared part the database worked out.

    Exactly what the live query computes in SQL: the terms are decimal numbers added without
    rounding, and the sum is floored once, after it has been scaled."""
    total = base
    if category in interest_categories:
        total = _EXACT.add(total, INTEREST_WEIGHT)
    if category in level_categories:
        total = _EXACT.add(total, LEVEL_WEIGHT)
    return int(_EXACT.to_integral_value(_EXACT.multiply(total, SCORE_SCALE)))


# The bonuses in whole points of the score. Each weight times the scale is a whole number, so
# floor((base + bonus) * scale) = floor(base * scale) + bonus points, exactly: a kept ranking
# floors each article's shared part once and adds whole numbers per person (F-50).
INTEREST_POINTS: Final = int(INTEREST_WEIGHT * SCORE_SCALE)
LEVEL_POINTS: Final = int(LEVEL_WEIGHT * SCORE_SCALE)
if INTEREST_POINTS != INTEREST_WEIGHT * SCORE_SCALE or LEVEL_POINTS != LEVEL_WEIGHT * SCORE_SCALE:
    raise RuntimeError("a bonus weight must be a whole number of score points")  # pragma: no cover


def scaled_base(base: Decimal) -> int:
    """The shared part of an article's score in whole points, floored: `person_score` for a
    person with no bonus on the article, and the start of every other person's score."""
    return int(_EXACT.to_integral_value(_EXACT.multiply(base, SCORE_SCALE)))


def score_ceiling(base: Decimal) -> int:
    """The highest score any article with this shared part (or less) can have for anybody."""
    return int(_EXACT.to_integral_value(_EXACT.multiply(_EXACT.add(base, MAX_BONUS), SCORE_SCALE)))


def rank_moment(now: datetime) -> datetime:
    """The moment the first page of the feed is ranked at: the start of the current five minutes."""
    return datetime.fromtimestamp(
        int(now.timestamp()) // RANK_MOMENT_SECONDS * RANK_MOMENT_SECONDS, tz=UTC
    )
