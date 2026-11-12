"""How many more insights today's budget covers (F-52): the estimate on the model page.

The numbers follow the gateway's own rules: an insight is refused when the day's total spending
would pass half of the cap, and each call is first booked at its worst case.
"""

import pytest

from pharos.llm import prompt
from pharos.llm.budget import INSIGHT_INPUT_TOKENS, INSIGHT_SHARE_PERCENT, insights_left
from pharos.llm.gateway import CHARS_PER_TOKEN, GatewayConfig
from pharos.llm.schemas import InsightAnswer
from pharos.llm.types import MAX_ANSWER_TOKENS, MAX_EXCERPT, MAX_TITLE, Stage, Task
from pharos.llm.types import mint_public_text as mint

ANSWER = MAX_ANSWER_TOKENS[Stage.INSIGHT]
DEFAULTS = {"price_in": 20, "price_out": 100, "answer_tokens": ANSWER}
BOOKING = INSIGHT_INPUT_TOKENS * 20 + ANSWER * 100  # 112,000 at the default prices


def test_the_booked_input_covers_the_longest_insight_prompt() -> None:
    article = mint("T" * MAX_TITLE, "E" * MAX_EXCERPT, "https://" + "h" * 60 + ".org/x", "en")
    messages = prompt.build(
        Task.INSIGHT, article, InsightAnswer, language="en", profession="p" * 40
    )
    booked_by_gateway = len(messages.system + messages.user) // CHARS_PER_TOKEN + 1
    assert booked_by_gateway <= INSIGHT_INPUT_TOKENS


def test_the_page_and_the_gateway_use_the_same_share() -> None:
    assert GatewayConfig(models={}).insight_share_percent == INSIGHT_SHARE_PERCENT == 50


def test_without_history_each_insight_counts_at_its_booking() -> None:
    left = insights_left(cap=2_000_000, spent=0, **DEFAULTS)
    assert (left.basis, left.cost_per_insight, left.ceiling) == ("estimate", BOOKING, 1_000_000)
    # 1,000,000 of room: 8 bookings of 112,000 fit (896,000), a ninth does not.
    assert left.count == 8


def test_recent_insights_set_the_cost_but_the_last_one_still_needs_its_booking() -> None:
    left = insights_left(
        cap=2_000_000, spent=0, measured_calls=10, measured_cost=400_000, **DEFAULTS
    )
    assert (left.basis, left.cost_per_insight) == ("measured", 40_000)
    # One booking of 112,000, then (1,000,000 - 112,000) // 40,000 = 22 more.
    assert left.count == 23


def test_what_the_other_stages_spent_today_counts_against_the_insights() -> None:
    assert insights_left(cap=2_000_000, spent=900_000, **DEFAULTS).count == 0
    assert insights_left(cap=2_000_000, spent=888_000, **DEFAULTS).count == 1
    # Past the share already (enrichment spent more than half): none, never a negative number.
    assert insights_left(cap=2_000_000, spent=1_500_000, **DEFAULTS).count == 0


@pytest.mark.parametrize("calls,cost", [(0, 0), (3, 0), (0, 500)])
def test_no_usable_history_falls_back_to_the_booking(calls: int, cost: int) -> None:
    left = insights_left(
        cap=2_000_000, spent=0, measured_calls=calls, measured_cost=cost, **DEFAULTS
    )
    assert (left.basis, left.cost_per_insight) == ("estimate", BOOKING)


def test_a_model_without_a_price_is_not_limited_by_the_budget() -> None:
    left = insights_left(cap=2_000_000, spent=0, price_in=0, price_out=0, answer_tokens=ANSWER)
    assert (left.count, left.basis, left.cost_per_insight) == (None, "free", 0)


def test_a_bigger_budget_covers_proportionally_more() -> None:
    small = insights_left(
        cap=2_000_000, spent=0, measured_calls=1, measured_cost=50_000, **DEFAULTS
    )
    big = insights_left(cap=20_000_000, spent=0, measured_calls=1, measured_cost=50_000, **DEFAULTS)
    assert (small.count, big.count) == (18, 198)
