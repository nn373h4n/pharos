"""The Celery side of "test the key": the task's name, its argument and what it leaves behind."""

import asyncio
import uuid
from typing import Any

import pytest

from pharos.core.config import Settings
from pharos.llm import keytest
from pharos.workers import app as worker_app
from pharos.workers import llm_tasks


class FakeRedis:
    def __init__(self) -> None:
        self.stored: dict[str, tuple[str, int]] = {}
        self.closed = False

    async def set(self, key: str, value: str, ex: int) -> None:
        self.stored[key] = (value, ex)

    async def aclose(self) -> None:
        self.closed = True


class FakeGateway:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.asked: list[uuid.UUID] = []

    async def check_key(self, org: uuid.UUID) -> str:
        self.asked.append(org)
        return self.outcome


def test_the_task_has_the_name_the_api_sends() -> None:
    assert llm_tasks.test_key_task.name == keytest.TASK_NAME
    celery = worker_app.create_celery(Settings(environment="test"))  # type: ignore[arg-type]
    assert keytest.TASK_NAME in celery.tasks


@pytest.mark.parametrize("argument", ["", "x", None, 5, "../../etc/passwd"])
def test_an_argument_that_is_not_an_identifier_does_nothing(
    monkeypatch: pytest.MonkeyPatch, argument: object
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("the database must not be touched")

    monkeypatch.setattr(llm_tasks.runtime, "run", forbidden)
    llm_tasks.test_key_task.run(argument)  # type: ignore[arg-type]


def run_task(
    monkeypatch: pytest.MonkeyPatch, *, outcome: str = "ok", build_fails: bool = False
) -> tuple[FakeRedis, FakeGateway, uuid.UUID, uuid.UUID]:
    redis, gateway, org = FakeRedis(), FakeGateway(outcome), uuid.uuid4()

    async def resolve(_connection: Any, _slug: str) -> uuid.UUID:
        return org

    class Begin:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: object) -> None:
            return None

    class Engine:
        def begin(self) -> Begin:
            return Begin()

    def build(*_a: object, **_k: object) -> FakeGateway:
        if build_fails:
            raise RuntimeError("no key")
        return gateway

    monkeypatch.setattr(llm_tasks, "get_settings", lambda: Settings(environment="test"))  # type: ignore[arg-type]
    monkeypatch.setattr(llm_tasks.repository, "resolve_org", resolve)
    monkeypatch.setattr(llm_tasks.aioredis, "from_url", lambda *_a, **_k: redis)
    monkeypatch.setattr(llm_tasks, "build_gateway", build)
    monkeypatch.setattr(llm_tasks, "DatabaseUsageSink", lambda _engine: object())
    monkeypatch.setattr(llm_tasks.runtime, "run", lambda work: asyncio.run(work(Engine())))  # type: ignore[arg-type]
    test = uuid.uuid4()
    llm_tasks.test_key_task.run(str(test))
    return redis, gateway, org, test


def test_the_answer_and_the_last_check_are_left_in_redis_and_the_connection_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, gateway, org, test = run_task(monkeypatch, outcome="rejected")
    assert gateway.asked == [org]
    assert set(redis.stored) == {keytest.result_key(test), keytest.last_key(org)}
    raw, ttl = redis.stored[keytest.result_key(test)]
    assert ttl == keytest.RESULT_TTL_SECONDS
    assert redis.stored[keytest.last_key(org)][1] == keytest.LAST_TTL_SECONDS
    parsed = keytest.parse_result(raw, test_id=test)
    assert parsed is not None and parsed.outcome == "rejected"
    assert redis.closed


def test_a_provider_that_cannot_be_built_is_reported_as_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, gateway, _org, test = run_task(monkeypatch, build_fails=True)
    assert gateway.asked == []
    parsed = keytest.parse_result(redis.stored[keytest.result_key(test)][0], test_id=test)
    assert parsed is not None and parsed.outcome == "not_configured"
    assert redis.closed
