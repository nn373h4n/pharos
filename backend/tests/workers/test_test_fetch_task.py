"""The Celery side of the test fetch: the task's name, its arguments and what it hands over."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from pharos.core.config import Settings
from pharos.ingest import testfetch
from pharos.workers import app as worker_app
from pharos.workers import ingest_tasks

GITHUB_TOKEN = "github_pat_11AAAAAAA0abcdefghijkl"


class FakeRedis:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_the_task_has_the_name_the_api_sends() -> None:
    assert ingest_tasks.test_fetch_task.name == testfetch.TASK_NAME == "pharos.ingest.test_fetch"
    celery = worker_app.create_celery(Settings(environment="test"))  # type: ignore[arg-type]
    assert testfetch.TASK_NAME in celery.tasks


@pytest.mark.parametrize(
    ("source", "test"),
    [
        ("", ""),
        ("x", "y"),
        (None, None),
        (5, 6),
        (str(uuid.uuid4()), "nope"),
        ("../../etc/passwd", str(uuid.uuid4())),
    ],
)
def test_arguments_that_are_not_two_identifiers_do_nothing(
    monkeypatch: pytest.MonkeyPatch, source: object, test: object
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("the database must not be touched")

    monkeypatch.setattr(ingest_tasks.runtime, "run", forbidden)
    ingest_tasks.test_fetch_task.run(source, test)  # type: ignore[arg-type]


def run_task(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> tuple[dict[str, object], FakeRedis]:
    seen: dict[str, object] = {}
    redis = FakeRedis()

    async def fake_run(engine: object, **kwargs: object) -> testfetch.TestResult:
        seen.update(kwargs)
        return testfetch.TestResult.model_validate(
            {
                "v": 1,
                "test_id": kwargs["test_id"],
                "source_id": kwargs["source_id"],
                "outcome": "ok",
            }
        )

    monkeypatch.setattr(ingest_tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(ingest_tasks.aioredis, "from_url", lambda *_a, **_k: redis)
    monkeypatch.setattr(ingest_tasks, "run_test_fetch", fake_run)
    monkeypatch.setattr(ingest_tasks.runtime, "run", lambda work: asyncio.run(work(None)))  # type: ignore[arg-type]
    source, test = uuid.uuid4(), uuid.uuid4()
    ingest_tasks.test_fetch_task.run(str(source), str(test))
    seen["expected"] = (source, test)
    return seen, redis


def test_the_task_hands_over_the_right_identifiers_and_closes_what_it_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(environment="test", org_slug="acme")  # type: ignore[arg-type]
    seen, redis = run_task(monkeypatch, settings)
    assert (seen["source_id"], seen["test_id"]) == seen["expected"]  # in this order, not swapped
    assert seen["slug"] == "acme"
    assert seen["store"] is redis and redis.closed
    assert callable(seen["clock"]) and seen["clock"]().tzinfo is UTC  # type: ignore[operator, attr-defined]
    assert isinstance(seen["clock"](), datetime)  # type: ignore[operator]


def test_the_task_builds_the_connectors_with_the_workers_github_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(environment="test", github_token=SecretStr(GITHUB_TOKEN))  # type: ignore[arg-type]
    seen, _ = run_task(monkeypatch, settings)
    connectors = seen["connectors"]
    assert set(connectors) == {"rss", "github", "hackernews"}  # type: ignore[arg-type]
    assert connectors["github"]._token == GITHUB_TOKEN  # type: ignore[index, attr-defined]
    anonymous, _ = run_task(monkeypatch, Settings(environment="test"))  # type: ignore[arg-type]
    assert anonymous["connectors"]["github"]._token is None  # type: ignore[index, attr-defined]
