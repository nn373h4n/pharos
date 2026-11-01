"""LLMGateway: the one door to a model (ADR-0002, ADR-0015, ADR-0016).

Application code never imports a provider library. A call goes through these steps, in this order,
and a failure at any step stops it before anything is sent:

1. the article must be `PublicArticleText` (class A of ADR-0015), the task one of ours, and a model
   must be configured for its stage;
2. a stage that keeps failing rests for a while;
3. the call is paced (calls per minute) and its estimated cost is booked against the day's cap;
4. the article is masked and put between markers as data; the model gets no tools;
5. one request with a deadline; a failure that cannot have been billed is retried a bounded number
   of times, each try booked again; one that may have been billed (a timeout, a broken connection,
   a cancelled task) keeps its booking, so the cap holds even when answers go missing;
6. the answer must be JSON that fits the task's schema, and is never repaired;
7. the booking is settled to the real cost, and a record with numbers only is written.

Prompts, answers and keys are never logged and never recorded.
"""

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

from pharos.core.clock import Clock
from pharos.llm import prompt
from pharos.llm.budget import Budget, Refusal, Reservation
from pharos.llm.errors import (
    BudgetExhausted,
    BudgetUnavailable,
    CallRateExceeded,
    LLMDisabled,
    LLMNotConfigured,
    NotPublicArticle,
    ProviderFailed,
    ProviderRejected,
    SchemaRejected,
    StagePaused,
)
from pharos.llm.provider import Provider
from pharos.llm.schemas import SCHEMAS, forbids_extra
from pharos.llm.types import MAX_ANSWER_TOKENS, TASK_STAGE, PublicArticleText, Stage, Task, Usage
from pharos.llm.usage import UsageRecord, UsageSink

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Micro-dollars per token, as (in, out): the same number as US dollars per million tokens. A model
# that is not listed is charged at the price in the configuration, which is conservative by default
# and which an operator raises for a dearer model (ADR-0016).
PRICES: dict[str, tuple[int, int]] = {}
DEFAULT_PRICE = (20, 100)  # 20 and 100 US dollars per million tokens
CHARS_PER_TOKEN = 3  # a pessimistic count: more tokens than the text really has

_FENCE = re.compile(r"^\s*```(?:json)?\s*(\{.*\})\s*```\s*$", re.DOTALL)

Event = Callable[[str, UUID], Awaitable[None]]

# The key check says nothing but this: no article, no schema, a few tokens.
KEY_TEST_SYSTEM = "You are a connectivity check. Answer with the single word: ok."
KEY_TEST_USER = "Answer now."


@dataclass(slots=True)
class Pause:
    """A stage that fails again and again rests (one process's view; enough to avoid a loop)."""

    after: int = 5
    seconds: float = 60.0
    failures: int = 0
    until: float = 0.0


@dataclass(slots=True)
class GatewayConfig:
    models: dict[Stage, str]
    calls_per_minute: int = 30
    timeout_seconds: float = 60.0
    max_retries: int = 2
    backoff_seconds: float = 1.0
    price_in: int = DEFAULT_PRICE[0]
    price_out: int = DEFAULT_PRICE[1]
    prices: dict[str, tuple[int, int]] = field(default_factory=lambda: dict(PRICES))


def _extract_json(text: str) -> str:
    """The JSON object of an answer: the whole text, or the whole text inside one code fence."""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    return fenced.group(1) if fenced else stripped


class LLMGateway:
    def __init__(
        self,
        provider: Provider | None,
        budget: Budget,
        usage: UsageSink,
        config: GatewayConfig,
        clock: Clock,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] | None = None,
        on_event: Event | None = None,
    ) -> None:
        self._provider = provider
        self._budget = budget
        self._usage = usage
        self._config = config
        self._clock = clock
        self._sleep = sleep
        self._on_event = on_event
        self._pauses: dict[Stage, Pause] = {}
        self._monotonic = monotonic or time.monotonic

    # --- the call ---

    async def generate(
        self,
        *,
        task: Task,
        org_id: UUID,
        article: PublicArticleText,
        schema: type[T],
        language: str | None = None,
        profession: str | None = None,
        professions: Sequence[str] | None = None,
    ) -> T:
        if not isinstance(article, PublicArticleText) or not article.is_genuine():
            raise NotPublicArticle("only PublicArticleText may be sent")
        if not isinstance(task, Task):
            raise TypeError("a task of the product is required")
        if SCHEMAS.get(task) is not schema:
            # The schema goes into the prompt, so it must be the product's own for this task.
            raise TypeError("the schema must be the one registered for the task")
        if not forbids_extra(schema):
            raise TypeError("the schema of an answer must forbid extra fields everywhere")
        stage = TASK_STAGE[task]
        provider = self._provider
        if provider is None:
            raise LLMDisabled()
        model = self._config.models.get(stage, "")
        if not model:
            raise LLMNotConfigured()
        self._check_pause(stage)

        messages = prompt.build(
            task,
            article,
            schema,
            language=language,
            profession=profession,
            professions=professions,
        )
        max_tokens = MAX_ANSWER_TOKENS[stage]
        estimate = self._estimate(model, messages.system + messages.user, max_tokens)

        attempts = 1 + max(0, self._config.max_retries)
        last: Exception | None = None
        for attempt in range(attempts):
            # Every request counts for the pace, a retry as much as the first try.
            if not await self._budget.pace(org_id, self._config.calls_per_minute):
                await self._record(org_id, stage, provider.name, model, Usage(0, 0, 0), "refused")
                raise CallRateExceeded()
            reservation = await self._book(org_id, estimate, stage, provider.name, model)
            try:
                answer = await provider.complete(
                    model=model,
                    system=messages.system,
                    user=messages.user,
                    max_tokens=max_tokens,
                    deadline_seconds=self._config.timeout_seconds,
                )
            except ProviderRejected:
                await self._budget.release(reservation)
                await self._record(org_id, stage, provider.name, model, Usage(0, 0, 0), "failed")
                self._failed(stage)
                raise
            except ProviderFailed as error:
                last = error
                if error.sent:
                    # It may have been billed: the booking stays, at the estimate.
                    await self._budget.settle(reservation, estimate)
                    await self._record(
                        org_id, stage, provider.name, model, Usage(0, 0, estimate), "failed"
                    )
                else:
                    await self._budget.release(reservation)
                if attempt + 1 < attempts:
                    await self._sleep(self._config.backoff_seconds * (2**attempt))
                continue
            except asyncio.CancelledError:
                # Cancelled in the middle: the request may have been sent, so the booking is NOT
                # given back. The record is written if it can be, shielded from the cancellation.
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.shield(
                        self._record(
                            org_id, stage, provider.name, model, Usage(0, 0, estimate), "failed"
                        )
                    )
                raise
            except Exception:
                # Not an error of the provider: a bug or a missing piece. Nothing is known to have
                # been sent, so the booking goes back; the failure is recorded and counts towards
                # the rest of the stage.
                await self._budget.release(reservation)
                await self._record(org_id, stage, provider.name, model, Usage(0, 0, 0), "failed")
                self._failed(stage)
                raise

            usage = Usage(
                answer.tokens_in,
                answer.tokens_out,
                self._cost(model, answer.tokens_in, answer.tokens_out),
            )
            try:
                await self._budget.settle(reservation, usage.cost_micro_usd)
            except BudgetUnavailable:
                # The call is paid for and the answer is good: do not lose it. The next call will
                # find the counter unreadable and stop (fail closed).
                logger.error("llm: the booking could not be settled")
            try:
                result = self._parse(answer.text, schema)
            except SchemaRejected:
                await self._record(org_id, stage, provider.name, model, usage, "rejected")
                self._failed(stage)
                raise
            await self._record(org_id, stage, provider.name, model, usage, "ok")
            self._worked(stage)
            logger.info(
                "llm call",
                extra={
                    "stage": stage.value,
                    "provider": provider.name,
                    "model": model,
                    "tokens_in": usage.tokens_in,
                    "tokens_out": usage.tokens_out,
                    "cost_micro_usd": usage.cost_micro_usd,
                    "masked": messages.masked,
                },
            )
            return result

        if last is None or not getattr(last, "sent", True):
            await self._record(org_id, stage, provider.name, model, Usage(0, 0, 0), "failed")
        self._failed(stage)
        raise last or ProviderFailed()

    async def check_key(self, org_id: UUID) -> str:
        """The key check, with a budget store that does not answer reported as a failure."""
        try:
            return await self._check_key(org_id)
        except BudgetUnavailable:
            return "failed"

    async def _check_key(self, org_id: UUID) -> str:
        """One tiny call to see whether the key in the worker works (the administrator's "test the
        key"). It carries no article and is booked, paced and recorded like any other call, with no
        retry. Returns a word of `keytest.Outcome`; it never raises for the reasons below."""
        provider = self._provider
        if provider is None:
            return "disabled"
        stage = Stage.KEY_TEST
        model = self._config.models.get(stage, "")
        if not model:
            return "not_configured"
        try:
            self._check_pause(stage)
            if not await self._budget.pace(org_id, self._config.calls_per_minute):
                await self._record(org_id, stage, provider.name, model, Usage(0, 0, 0), "refused")
                return "paced"
            estimate = self._estimate(
                model, KEY_TEST_SYSTEM + KEY_TEST_USER, MAX_ANSWER_TOKENS[stage]
            )
            reservation = await self._book(org_id, estimate, stage, provider.name, model)
        except StagePaused:
            return "paused"
        except BudgetExhausted:
            return "budget"
        try:
            answer = await provider.complete(
                model=model,
                system=KEY_TEST_SYSTEM,
                user=KEY_TEST_USER,
                max_tokens=MAX_ANSWER_TOKENS[stage],
                deadline_seconds=self._config.timeout_seconds,
            )
        except ProviderRejected:
            await self._budget.release(reservation)
            await self._record(org_id, stage, provider.name, model, Usage(0, 0, 0), "failed")
            self._failed(stage)
            return "rejected"
        except ProviderFailed as error:
            if error.sent:
                await self._budget.settle(reservation, estimate)
                await self._record(
                    org_id, stage, provider.name, model, Usage(0, 0, estimate), "failed"
                )
            else:
                await self._budget.release(reservation)
                await self._record(org_id, stage, provider.name, model, Usage(0, 0, 0), "failed")
            self._failed(stage)
            return "failed"
        except LLMNotConfigured:
            await self._budget.release(reservation)
            return "not_configured"
        usage = Usage(
            answer.tokens_in,
            answer.tokens_out,
            self._cost(model, answer.tokens_in, answer.tokens_out),
        )
        with contextlib.suppress(BudgetUnavailable):
            await self._budget.settle(reservation, usage.cost_micro_usd)
        await self._record(org_id, stage, provider.name, model, usage, "ok")
        self._worked(stage)
        return "ok"

    # --- parts ---

    def _price(self, model: str) -> tuple[int, int]:
        return self._config.prices.get(model, (self._config.price_in, self._config.price_out))

    def _estimate(self, model: str, text: str, max_tokens: int) -> int:
        price_in, price_out = self._price(model)
        tokens_in = len(text) // CHARS_PER_TOKEN + 1
        return tokens_in * price_in + max_tokens * price_out

    def _cost(self, model: str, tokens_in: int, tokens_out: int) -> int:
        price_in, price_out = self._price(model)
        return max(0, tokens_in) * price_in + max(0, tokens_out) * price_out

    async def _book(
        self, org_id: UUID, estimate: int, stage: Stage, provider: str, model: str
    ) -> Reservation:
        outcome = await self._budget.reserve(org_id, estimate)
        if isinstance(outcome, Refusal):
            if outcome.exhausted_first:
                await self._event("llm.budget_exhausted", org_id)
            await self._record(org_id, stage, provider, model, Usage(0, 0, 0), "refused")
            raise BudgetExhausted()
        if outcome.warned_first:
            await self._event("llm.budget_warning", org_id)
        return outcome

    @staticmethod
    def _parse(text: str, schema: type[T]) -> T:
        try:
            return schema.model_validate_json(_extract_json(text))
        except (ValidationError, ValueError, json.JSONDecodeError):
            raise SchemaRejected() from None

    async def _record(
        self, org_id: UUID, stage: Stage, provider: str, model: str, usage: Usage, outcome: str
    ) -> None:
        try:
            await self._usage.record(
                UsageRecord(
                    org_id,
                    stage.value,
                    provider,
                    model,
                    usage.tokens_in,
                    usage.tokens_out,
                    usage.cost_micro_usd,
                    outcome,
                )
            )
        except Exception:
            logger.exception("llm: the usage record could not be written")

    async def _event(self, name: str, org_id: UUID) -> None:
        logger.warning("llm budget", extra={"event": name})
        if self._on_event is not None:
            try:
                await self._on_event(name, org_id)
            except Exception:
                logger.exception("llm: the event could not be recorded", extra={"event": name})

    # --- a stage that keeps failing rests ---

    def _pause(self, stage: Stage) -> Pause:
        return self._pauses.setdefault(stage, Pause())

    def _check_pause(self, stage: Stage) -> None:
        pause = self._pause(stage)
        if pause.until > self._monotonic():
            raise StagePaused()

    def _failed(self, stage: Stage) -> None:
        pause = self._pause(stage)
        pause.failures += 1
        if pause.failures >= pause.after:
            pause.until = self._monotonic() + pause.seconds
            pause.failures = 0

    def _worked(self, stage: Stage) -> None:
        self._pause(stage).failures = 0
