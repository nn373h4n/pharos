"""The Celery side of an insight: arguments, when it does nothing, and what it hands over."""

import asyncio
import uuid
from typing import Any

import pytest

from pharos.core.config import Settings
from pharos.llm.errors import BudgetExhausted
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
