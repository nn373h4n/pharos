"""The Celery side of the enrichment sweep: when it does nothing, the lock, what it hands over."""

import asyncio
import uuid
from typing import Any

import pytest

from pharos.core.config import Settings
from pharos.llm import enrichment
from pharos.workers import app as worker_app
from pharos.workers import llm_tasks


class FakeRedis:
    def __init__(self, *, locked: bool = False) -> None:
        self.locked = locked
        self.unlocked: list[tuple[str, str]] = []
        self.token: str | None = None
        self.closed = False

    async def set(self, key: str, value: str, ex: int, nx: bool) -> bool:
        assert key == llm_tasks.SWEEP_LOCK and nx and ex == llm_tasks.SWEEP_LOCK_SECONDS
        self.token = value
        return not self.locked

    async def eval(self, script: str, keys: int, key: str, token: str) -> int:
        assert script == llm_tasks._UNLOCK and keys == 1
        self.unlocked.append((key, token))
        return 1

    async def aclose(self) -> None:
        self.closed = True


def test_the_task_is_registered_and_scheduled_every_minute() -> None:
    celery = worker_app.create_celery(Settings(environment="test"))  # type: ignore[arg-type]
    assert llm_tasks.SWEEP_TASK in celery.tasks
    assert celery.conf.beat_schedule["llm-enrich-sweep"] == {
        "task": llm_tasks.SWEEP_TASK,
        "schedule": 60.0,
    }


def run_task(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: str = "fake",
    locked: bool = False,
    build_fails: bool = False,
    org: uuid.UUID | None = None,
) -> tuple[FakeRedis, list[dict[str, Any]], list[str]]:
    redis, swept, touched = FakeRedis(locked=locked), [], []
    settings = Settings(environment="test", llm_provider=provider, llm_enrich_batch=7)  # type: ignore[arg-type]
    found = org if org is not None else uuid.uuid4()

    async def resolve(_connection: Any, _slug: str) -> uuid.UUID:
        touched.append("database")
        return found

    class Begin:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: object) -> None:
            return None

    class Engine:
        def begin(self) -> Begin:
            return Begin()

    async def sweep(_engine: Any, _gateway: Any, organization: uuid.UUID, **kwargs: Any) -> Any:
        swept.append({"org": organization, **kwargs})
        return enrichment.SweepResult()

    def build(*_a: object, **_k: object) -> object:
        if build_fails:
            raise RuntimeError("no key")
        return object()

    monkeypatch.setattr(llm_tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(llm_tasks.repository, "resolve_org", resolve)
    monkeypatch.setattr(llm_tasks.aioredis, "from_url", lambda *_a, **_k: redis)
    monkeypatch.setattr(llm_tasks, "build_gateway", build)
    monkeypatch.setattr(llm_tasks, "DatabaseUsageSink", lambda _engine: object())
    monkeypatch.setattr(llm_tasks.enrichment, "sweep", sweep)
    monkeypatch.setattr(llm_tasks.runtime, "run", lambda work: asyncio.run(work(Engine())))  # type: ignore[arg-type]
    llm_tasks.enrich_sweep_task.run()
    return redis, swept, touched


def test_a_sweep_hands_the_batch_and_the_organization_over_and_gives_the_lock_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = uuid.uuid4()
    redis, swept, _ = run_task(monkeypatch, org=org)
    [call] = swept
    assert (call["org"], call["batch"]) == (org, 7)
    assert redis.unlocked == [(llm_tasks.SWEEP_LOCK, redis.token)] and redis.closed
    assert redis.token and len(redis.token) == 32  # a value of its own, not a flag


def test_with_the_model_off_nothing_is_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, swept, touched = run_task(monkeypatch, provider="off")
    assert swept == [] and touched == [] and not redis.closed


def test_a_second_sweep_while_one_runs_does_nothing_and_leaves_the_lock_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, swept, _ = run_task(monkeypatch, locked=True)
    assert swept == [] and redis.unlocked == [] and redis.closed


def test_a_provider_that_cannot_be_built_ends_the_sweep_and_still_gives_the_lock_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, swept, _ = run_task(monkeypatch, build_fails=True)
    assert swept == [] and redis.unlocked == [(llm_tasks.SWEEP_LOCK, redis.token)]
    assert redis.closed
