"""The Celery side of ingestion: names, schedule and what travels through the broker."""

import asyncio
import uuid

import pytest

from pharos.core.config import Settings
from pharos.ingest.dispatcher import LEASE_SECONDS
from pharos.workers import app as worker_app
from pharos.workers import ingest_tasks


def test_the_tasks_have_fixed_names() -> None:
    assert ingest_tasks.dispatch_task.name == "pharos.ingest.dispatch"
    assert ingest_tasks.fetch_task.name == "pharos.ingest.fetch"
    assert ingest_tasks.cleanup_task.name == "pharos.ingest.cleanup"


def test_beat_dispatches_every_minute_and_cleans_up_a_few_times_a_day() -> None:
    schedule = worker_app.create_celery(Settings(environment="test")).conf.beat_schedule  # type: ignore[arg-type]
    assert schedule["ingest-dispatch"] == {"task": "pharos.ingest.dispatch", "schedule": 60.0}
    assert schedule["ingest-cleanup"]["task"] == "pharos.ingest.cleanup"
    assert 3600 <= schedule["ingest-cleanup"]["schedule"] <= 24 * 3600
    assert "heartbeat" in schedule


def test_only_two_identifiers_travel_through_the_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(
        ingest_tasks.fetch_task, "apply_async", lambda **kwargs: sent.append(kwargs)
    )
    source, token = uuid.uuid4(), uuid.uuid4()
    ingest_tasks.enqueue_fetch(source, token)
    # Dropped once it has waited so long that a fetch could no longer finish inside its lease.
    assert sent == [{"args": [str(source), str(token)], "expires": LEASE_SECONDS - 160}]
    assert LEASE_SECONDS - 160 > 0


@pytest.mark.parametrize("switch", [True, False])
def test_the_dispatcher_is_told_whether_personal_feeds_are_on(
    monkeypatch: pytest.MonkeyPatch, switch: bool
) -> None:
    told: list[bool] = []

    async def fake_dispatch(engine: object, **kwargs: object) -> object:
        told.append(bool(kwargs["personal"]))
        return type("Report", (), {"leased": 0})()

    monkeypatch.setattr(ingest_tasks, "dispatch", fake_dispatch)
    monkeypatch.setattr(
        ingest_tasks,
        "get_settings",
        lambda: Settings(environment="test", personal_feeds=switch),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(ingest_tasks.runtime, "run", lambda work: asyncio.run(work(object())))  # type: ignore[arg-type]
    ingest_tasks.dispatch_task.run()
    assert told == [switch]


@pytest.mark.parametrize(
    ("source", "token"),
    [
        ("https://attacker.example/feed", str(uuid.uuid4())),
        (str(uuid.uuid4()), "not-a-uuid"),
        ("", ""),
        (None, None),
        (5, 6),
        ("../../etc/passwd", "x"),
    ],
)
def test_a_fetch_task_with_anything_but_identifiers_does_nothing(
    monkeypatch: pytest.MonkeyPatch, source: object, token: object
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("the database must not be touched")

    monkeypatch.setattr(ingest_tasks.runtime, "run", forbidden)
    ingest_tasks.fetch_task.run(source, token)  # type: ignore[arg-type]


def test_the_lease_outlives_the_longest_task() -> None:
    conf = worker_app.create_celery(Settings(environment="test")).conf  # type: ignore[arg-type]
    assert conf.task_time_limit < LEASE_SECONDS
