import pytest
from celery import signals

from pharos.core.config import Settings
from pharos.core.heartbeat import HEARTBEAT_INTERVAL_SECONDS
from pharos.workers import app as worker_app
from pharos.workers.app import create_celery
from pharos.workers.tasks import HEARTBEAT_TASK

REDIS_URL = "redis://:Qw7-example-only@cache:6379/3"


def settings() -> Settings:
    return Settings(environment="test", redis_url=REDIS_URL)  # type: ignore[arg-type]


def test_the_broker_is_the_configured_redis() -> None:
    assert create_celery(settings()).conf.broker_url == REDIS_URL


def test_the_heartbeat_task_is_registered_and_scheduled_every_minute() -> None:
    app = create_celery(settings())
    assert HEARTBEAT_TASK in app.tasks
    assert app.conf.beat_schedule["heartbeat"] == {
        "task": HEARTBEAT_TASK,
        "schedule": float(HEARTBEAT_INTERVAL_SECONDS),
    }
    assert set(app.conf.beat_schedule) == {
        "heartbeat",
        "ingest-dispatch",
        "ingest-cleanup",
        "llm-enrich-sweep",
    }
    assert HEARTBEAT_INTERVAL_SECONDS == 60


def test_tasks_are_safe_to_retry_and_use_json_only() -> None:
    conf = create_celery(settings()).conf
    assert conf.task_acks_late is True  # redelivered if the worker dies
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_serializer == "json"
    assert list(conf.accept_content) == ["json"]  # no pickle
    assert conf.task_ignore_result is True
    assert conf.broker_connection_retry_on_startup is True


def test_signal_handlers_are_registered_once_however_often_the_factory_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(worker_app, "write_heartbeat", lambda: calls.append("heartbeat"))
    create_celery(settings())
    create_celery(settings())
    signals.worker_ready.send(sender=None)
    assert calls == ["heartbeat"]  # the first heartbeat is written as soon as the worker is ready


def test_worker_logging_is_configured_through_the_pharos_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    levels: list[str] = []
    monkeypatch.setattr(worker_app, "configure_logging", levels.append)
    monkeypatch.setattr(
        worker_app,
        "get_settings",
        lambda: Settings(environment="test", log_level="DEBUG"),
    )
    signals.setup_logging.send(sender=None, loglevel=10, logfile=None, format="", colorize=False)
    assert levels == ["DEBUG"]
