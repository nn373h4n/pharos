"""Runs real Celery workers against the real Redis broker."""

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator

import pytest
from celery.contrib.testing.worker import start_worker
from redis import Redis

from pharos.core.config import get_settings
from pharos.core.heartbeat import HEARTBEAT_KEY
from pharos.workers.app import create_celery
from pharos.workers.tasks import heartbeat

pytestmark = pytest.mark.integration


@pytest.fixture
def worker_env(monkeypatch: pytest.MonkeyPatch, redis_worker_url: str) -> Iterator[dict[str, str]]:
    """The worker and its tasks read their settings from the environment, as in production.

    They use the worker's own Redis user, so a passing test also proves that user can do
    everything Celery needs (queue, kombu bookkeeping, the heartbeat key).
    """
    monkeypatch.setenv("PHAROS_ENVIRONMENT", "test")
    monkeypatch.setenv("PHAROS_REDIS_URL", redis_worker_url)
    get_settings.cache_clear()
    yield {**os.environ, "PHAROS_ENVIRONMENT": "test", "PHAROS_REDIS_URL": redis_worker_url}
    get_settings.cache_clear()


@pytest.fixture
def redis_client(redis_url: str) -> Iterator[Redis]:
    client = Redis.from_url(redis_url)
    client.delete(HEARTBEAT_KEY)
    yield client
    client.delete(HEARTBEAT_KEY)
    client.close()


def wait_for[T](read: Callable[[], T | None], timeout: float = 30.0) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value is not None:
            return value
        time.sleep(0.1)
    pytest.fail(f"condition not met within {timeout} seconds")


def stored_heartbeat(client: Redis) -> float | None:
    raw = client.get(HEARTBEAT_KEY)
    return None if raw is None else float(raw)  # type: ignore[arg-type]


@pytest.mark.usefixtures("worker_env")
def test_worker_runs_the_heartbeat_task_delivered_by_the_broker(redis_client: Redis) -> None:
    app = create_celery(get_settings())
    assert stored_heartbeat(redis_client) is None
    with start_worker(app, pool="solo", perform_ping_check=False, loglevel="WARNING"):
        # What beat does every minute: put the task on the broker for a worker to pick up.
        heartbeat.apply_async()
        first = wait_for(lambda: stored_heartbeat(redis_client))

        time.sleep(0.05)  # make the second timestamp clearly newer
        heartbeat.apply_async()
        second = wait_for(
            lambda: v if (v := stored_heartbeat(redis_client)) and v > first else None
        )
    assert second > first
    assert abs(second - time.time()) < 60


def test_a_started_worker_process_records_a_heartbeat_immediately(
    redis_client: Redis, worker_env: dict[str, str]
) -> None:
    # The CLI worker (not the embedded one) sends worker_ready, which writes the first heartbeat so
    # the API does not report a missing worker during the first minute after a start.
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "--app",
            "pharos.workers.entrypoint",
            "worker",
            "--pool",
            "solo",
            "--without-gossip",
            "--without-mingle",
            "--loglevel",
            "WARNING",
        ],
        env=worker_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        recorded = wait_for(lambda: stored_heartbeat(redis_client))
    finally:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
    assert abs(recorded - time.time()) < 60, output
