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
does not reorder itself while someone reads it.
"""

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
