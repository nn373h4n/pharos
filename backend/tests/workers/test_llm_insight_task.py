"""The Celery side of an insight: arguments, when it does nothing, and what it hands over."""

import asyncio
import uuid
from typing import Any

import pytest
from celery.exceptions import MaxRetriesExceededError

from pharos.core.config import Settings
from pharos.llm.errors import BudgetExhausted, CallRateExceeded, StagePaused
from pharos.workers import app as worker_app
from pharos.workers import llm_tasks


class FakeRedis:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_the_task_has_the_name_the_api_sends() -> None:
    assert llm_tasks.insight_task.name == llm_tasks.INSIGHT_TASK == "pharos.llm.insight"
    celery = worker_app.create_celery(Settings(environment="test"))  # type: ignore[arg-type]
    assert llm_tasks.INSIGHT_TASK in celery.tasks


@pytest.mark.parametrize(
    ("item", "profession", "language"),
    [("", "accountant", "en"), ("x", "accountant", "en"), (None, "a", "en"), (5, "a", "en")],
)
def test_an_argument_that_is_not_an_identifier_does_nothing(
    monkeypatch: pytest.MonkeyPatch, item: object, profession: str, language: str
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("the database must not be touched")

    monkeypatch.setattr(llm_tasks.runtime, "run", forbidden)
    llm_tasks.insight_task.run(item, profession, language)  # type: ignore[arg-type]


def run_task(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: str = "fake",
    build_fails: bool = False,
    outcome: str | Exception = "ok",
    profession: Any = "accountant",
    language: Any = "az",
) -> tuple[FakeRedis, list[tuple[Any, ...]], list[str]]:
    redis, made, touched = FakeRedis(), [], []
    settings = Settings(environment="test", llm_provider=provider)  # type: ignore[arg-type]
    org, item = uuid.uuid4(), uuid.uuid4()

    async def resolve(_connection: Any, _slug: str) -> uuid.UUID:
        touched.append("database")
        return org

    class Begin:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: object) -> None:
            return None

    class Engine:
        def begin(self) -> Begin:
            return Begin()

    async def make(_engine: Any, _gateway: Any, *args: Any) -> str:
        made.append(args)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def build(*_a: object, **_k: object) -> object:
        if build_fails:
            raise RuntimeError("no key")
        return object()

    monkeypatch.setattr(llm_tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(llm_tasks.repository, "resolve_org", resolve)
    monkeypatch.setattr(llm_tasks.aioredis, "from_url", lambda *_a, **_k: redis)
    monkeypatch.setattr(llm_tasks, "build_gateway", build)
    monkeypatch.setattr(llm_tasks, "DatabaseUsageSink", lambda _engine: object())
    monkeypatch.setattr(llm_tasks.insights, "make_insight", make)
    monkeypatch.setattr(llm_tasks.runtime, "run", lambda work: asyncio.run(work(Engine())))  # type: ignore[arg-type]
    llm_tasks.insight_task.run(str(item), profession, language)
    made_with = [(args[0], args[1], args[2], args[3]) for args in made]
    assert all(args[1] == item for args in made)
    return redis, made_with, touched


def test_the_task_hands_over_the_identifiers_and_closes_what_it_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, made, _ = run_task(monkeypatch)
    [(org, item, profession, language)] = made
    assert isinstance(org, uuid.UUID) and isinstance(item, uuid.UUID)
    assert (profession, language) == ("accountant", "az") and redis.closed


def test_with_the_model_off_nothing_is_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, made, touched = run_task(monkeypatch, provider="off")
    assert made == [] and touched == [] and not redis.closed


def test_a_provider_that_cannot_be_built_makes_nothing_and_closes_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, made, _ = run_task(monkeypatch, build_fails=True)
    assert made == [] and redis.closed


def test_a_spent_budget_or_a_resting_stage_ends_the_task_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, made, _ = run_task(monkeypatch, outcome=BudgetExhausted())
    assert len(made) == 1 and redis.closed  # it was tried; nothing was raised out of the task


# --- the calls of this minute are spent (F-51) ---------------------------------------------


class Retried(Exception):
    """Stands for Celery's Retry, so a test can see what the task asked for."""


def retry_calls(monkeypatch: pytest.MonkeyPatch, *, exhausted: bool = False) -> list[float]:
    asked: list[float] = []

    def retry(*_a: object, countdown: float = 0, **_k: object) -> Exception:
        asked.append(countdown)
        if exhausted:
            raise MaxRetriesExceededError()
        return Retried()

    monkeypatch.setattr(llm_tasks.insight_task, "retry", retry)
    return asked


def test_when_the_minutes_calls_are_spent_the_task_tries_again_at_the_next_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = retry_calls(monkeypatch)
    with pytest.raises(Retried):
        run_task(monkeypatch, outcome=CallRateExceeded())
    [wait] = asked
    assert 1 < wait <= 70  # the rest of this minute, and a few seconds more
    assert llm_tasks.INSIGHT_RETRIES * 70 < 300  # every try ends before the marker does


@pytest.mark.parametrize("stop", [BudgetExhausted(), StagePaused()])
def test_other_reasons_to_stop_are_not_tried_again(
    monkeypatch: pytest.MonkeyPatch, stop: Exception
) -> None:
    """A spent budget or a stage that keeps failing will not be better in a minute."""
    asked = retry_calls(monkeypatch)
    run_task(monkeypatch, outcome=stop)
    assert asked == []


def test_a_task_that_has_used_its_tries_ends_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = retry_calls(monkeypatch, exhausted=True)
    run_task(monkeypatch, outcome=CallRateExceeded())  # nothing is raised out of the task
    assert len(asked) == 1


def test_a_task_that_made_its_insight_does_not_try_again(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = retry_calls(monkeypatch)
    run_task(monkeypatch, outcome="ok")
    assert asked == []
