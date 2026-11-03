"""Embeddings (M4-05, ADR-0007): what is embedded, how an answer is checked, and that the call goes
through the same budget, pace, pause and record as any other model call."""

import math
from typing import Any

import pytest

from pharos.llm.embedding import (
    DEMO_DIM,
    DemoEmbedder,
    EmbedAnswer,
    check_vector,
    embedding_text,
)
from pharos.llm.errors import (
    BudgetExhausted,
    CallRateExceeded,
    LLMDisabled,
    LLMNotConfigured,
    NotPublicArticle,
    ProviderFailed,
    ProviderRejected,
    SchemaRejected,
    StagePaused,
)
from pharos.llm.provider import FakeProvider
from pharos.llm.types import Stage, mint_public_text

from .support import GOOD, ORG, Harness, MemoryBudget, article

pytestmark = pytest.mark.anyio


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class Scripted:
    """An embedder that answers as told."""

    name = "scripted"

    def __init__(self, outcome: Any = None) -> None:
        self.outcome = outcome if outcome is not None else EmbedAnswer([[0.5] * 16], tokens_in=40)
        self.calls: list[dict[str, Any]] = []

    async def embed(self, *, model: str, texts: list[str], deadline_seconds: float) -> EmbedAnswer:
        self.calls.append({"model": model, "texts": texts, "deadline": deadline_seconds})
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome  # type: ignore[no-any-return]


def harness(embedder: Any, **kwargs: Any) -> Harness:
    return Harness(None, embedder=embedder, **kwargs)


class TestWhatIsEmbedded:
    def test_it_is_the_masked_title_and_excerpt_and_nothing_else(self) -> None:
        text = embedding_text(
            mint_public_text(
                "Mail jane.doe@example.org about it",
                "Call +994 50 123 45 67 now",
                "https://a.example/x",
                "en",
            )
        )
        assert "jane.doe@example.org" not in text and "[EMAIL]" in text
        assert "123 45 67" not in text
        assert text.count("\n") == 1

    def test_it_is_cut_to_the_bounds_of_a_prompt(self) -> None:
        text = embedding_text(article(title="t" * 5000, excerpt="e" * 5000))
        assert len(text) < 1000


class TestTheAnswerIsData:
    def test_a_good_vector_is_accepted(self) -> None:
        assert check_vector(EmbedAnswer([[0.1] * 8], 1)) == (0.1,) * 8
        assert len(check_vector(EmbedAnswer([[1] * 4096], 1))) == 4096  # integers are numbers

    @pytest.mark.parametrize(
        "answer",
        [
            EmbedAnswer([], 1),
            EmbedAnswer([[0.1] * 8, [0.1] * 8], 1),  # more than we sent
            EmbedAnswer([[0.1] * 7], 1),  # too short
            EmbedAnswer([[0.1] * 4097], 1),  # too long
            EmbedAnswer([[0.0] * 8], 1),  # nothing in it
            EmbedAnswer([[1e-50] * 8], 1),  # becomes zeros in the database's 32-bit floats
            EmbedAnswer([[1e-31] * 8], 1),
            EmbedAnswer([[3.5e38] + [0.1] * 7], 1),  # too big for 32 bits
            EmbedAnswer([[1e300] + [0.1] * 7], 1),
            EmbedAnswer([[0.1] * 7 + [math.nan]], 1),
            EmbedAnswer([[0.1] * 7 + [math.inf]], 1),
            EmbedAnswer([[0.1] * 7 + [-math.inf]], 1),
            EmbedAnswer([[0.1] * 7 + [True]], 1),  # a bool is not a number here
            EmbedAnswer([[0.1] * 7 + ["0.1"]], 1),  # type: ignore[list-item]
            EmbedAnswer([[0.1] * 7 + [None]], 1),  # type: ignore[list-item]
            EmbedAnswer(["0.1" * 8], 1),  # type: ignore[list-item]
        ],
    )
    def test_anything_else_is_refused_not_stored(self, answer: EmbedAnswer) -> None:
        with pytest.raises(SchemaRejected):
            check_vector(answer)


class TestTheDemoEmbedder:
    async def embed(self, *texts: str) -> list[list[float]]:
        return (
            await DemoEmbedder().embed(model="m", texts=list(texts), deadline_seconds=1)
        ).vectors

    async def test_it_is_deterministic_and_normalised(self) -> None:
        [a], [b] = await self.embed("Acme ships Widget 2"), await self.embed("Acme ships Widget 2")
        assert a == b and len(a) == DEMO_DIM
        assert math.isclose(cosine(a, a), 1.0, abs_tol=1e-9)

    async def test_a_rewritten_copy_is_close_and_another_story_is_far(self) -> None:
        [a, b, c] = await self.embed(
            "Acme ships Widget 2 with faster sync for finance teams",
            "Finance teams get faster sync: Acme ships Widget 2",
            "City council approves the new bicycle lane budget",
        )
        assert cosine(a, b) > 0.6 > 0.3 > cosine(a, c)

    async def test_an_empty_text_still_gives_a_usable_vector(self) -> None:
        [v] = await self.embed("")
        assert check_vector(EmbedAnswer([v], 0)) is not None


class TestTheCall:
    async def test_a_good_call_returns_the_vector_and_is_recorded_as_numbers(self) -> None:
        embedder = Scripted()
        rig = harness(embedder)
        got = await rig.gateway.embed(org_id=ORG, article=article())
        assert got.model == "model-embedding" and got.dim == 16
        [record] = rig.usage.records
        assert (record.stage, record.provider, record.outcome) == ("embedding", "scripted", "ok")
        assert record.tokens_in == 40 and record.tokens_out == 0 and record.cost_micro_usd == 40
        [call] = embedder.calls
        assert len(call["texts"]) == 1 and call["model"] == "model-embedding"
        # the booking is replaced by what the call really cost
        assert rig.budget.settled == [40]

    async def test_only_public_article_text_is_embedded(self) -> None:
        rig = harness(Scripted())
        with pytest.raises(NotPublicArticle):
            await rig.gateway.embed(org_id=ORG, article="a string")  # type: ignore[arg-type]

    async def test_it_is_off_without_an_embedder_and_unconfigured_without_a_model(self) -> None:
        with pytest.raises(LLMDisabled):
            await harness(None).gateway.embed(org_id=ORG, article=article())
        rig = harness(Scripted(), models={Stage.FILTER: "x"})
        with pytest.raises(LLMNotConfigured):
            await rig.gateway.embed(org_id=ORG, article=article())

    async def test_a_local_embedder_costs_nothing(self) -> None:
        rig = harness(Scripted(), embedding_price=0)
        await rig.gateway.embed(org_id=ORG, article=article())
        assert rig.usage.records[0].cost_micro_usd == 0 and rig.budget.reserved == [0]

    async def test_the_budget_is_asked_first_and_a_full_day_stops_the_call(self) -> None:
        embedder = Scripted()
        rig = harness(embedder, budget=MemoryBudget(cap=1))
        with pytest.raises(BudgetExhausted):
            await rig.gateway.embed(org_id=ORG, article=article())
        assert embedder.calls == []  # nothing was sent
        assert rig.usage.records[0].outcome == "refused"

    async def test_the_pace_limit_stops_the_call(self) -> None:
        embedder = Scripted()
        rig = harness(embedder, budget=MemoryBudget(calls_per_minute=0))
        with pytest.raises(CallRateExceeded):
            await rig.gateway.embed(org_id=ORG, article=article())
        assert embedder.calls == [] and rig.usage.records[0].outcome == "refused"

    async def test_a_refused_request_gives_the_booking_back_and_counts_as_a_failure(self) -> None:
        rig = harness(Scripted(ProviderRejected()))
        with pytest.raises(ProviderRejected):
            await rig.gateway.embed(org_id=ORG, article=article())
        assert rig.budget.released == 1 and rig.budget.total == 0
        assert rig.usage.records[0].outcome == "failed"

    async def test_a_failure_that_may_have_been_billed_keeps_the_estimate(self) -> None:
        rig = harness(Scripted(ProviderFailed(sent=True)))
        with pytest.raises(ProviderFailed):
            await rig.gateway.embed(org_id=ORG, article=article())
        assert rig.budget.released == 0 and rig.budget.total > 0
        assert rig.usage.records[0].cost_micro_usd > 0
        rig2 = harness(Scripted(ProviderFailed(sent=False)))
        with pytest.raises(ProviderFailed):
            await rig2.gateway.embed(org_id=ORG, article=article())
        assert rig2.budget.released == 1 and rig2.budget.total == 0  # control: not sent, not billed

    async def test_a_provider_that_reports_no_tokens_does_not_make_the_call_free(self) -> None:
        rig = harness(Scripted(EmbedAnswer([[0.5] * 16], tokens_in=0)))
        await rig.gateway.embed(org_id=ORG, article=article())
        assert rig.usage.records[0].tokens_in > 0 and rig.usage.records[0].cost_micro_usd > 0

    async def test_embeddings_are_counted_in_a_lane_and_against_a_limit_of_their_own(self) -> None:
        rig = harness(Scripted(), embedding_calls_per_minute=7, calls_per_minute=3)
        await rig.gateway.embed(org_id=ORG, article=article())
        assert rig.budget.lanes == ["embedding"] and rig.budget.limits == [7]
        # Positive control: a text call is counted in the default lane against its own limit.
        rig2 = Harness(FakeProvider(GOOD), calls_per_minute=3)
        await rig2.run()
        assert rig2.budget.lanes == [""] and rig2.budget.limits == [3]

    async def test_a_bad_vector_is_rejected_recorded_and_never_returned(self) -> None:
        rig = harness(Scripted(EmbedAnswer([[0.1] * 7 + [math.nan]], 30)))
        with pytest.raises(SchemaRejected):
            await rig.gateway.embed(org_id=ORG, article=article())
        [record] = rig.usage.records
        assert record.outcome == "rejected" and record.tokens_in == 30  # paid for, so booked

    async def test_a_stage_that_keeps_failing_rests(self) -> None:
        rig = harness(Scripted(ProviderRejected()))
        for _ in range(rig.gateway._pause(Stage.EMBEDDING).after):
            with pytest.raises(ProviderRejected):
                await rig.gateway.embed(org_id=ORG, article=article())
        with pytest.raises(StagePaused):
            await rig.gateway.embed(org_id=ORG, article=article())
