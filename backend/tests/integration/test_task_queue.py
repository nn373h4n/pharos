"""What the API's queue client really puts on the broker (ADR-0013), and what it does when the broker
is down."""

import base64
import json
import time
import uuid
from collections.abc import Iterator

import pytest
from redis import Redis

from pharos.core.errors import DependencyUnavailable
from pharos.core.queue import CeleryQueue
from pharos.ingest import testfetch

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def api(redis_url: str) -> Iterator[Redis]:
    client = Redis.from_url(redis_url)
    yield client
    client.close()


async def test_the_task_on_the_broker_is_a_name_and_two_identifiers(
    api: Redis, redis_url: str
) -> None:
    source, test = str(uuid.uuid4()), str(uuid.uuid4())
    before = api.llen("celery")
    await CeleryQueue(redis_url).send(testfetch.TASK_NAME, [source, test], expires=60)
    assert api.llen("celery") == before + 1
    raw = api.rpop("celery")  # ours is the last one pushed on the left: take it off again
    assert raw is not None
    message = json.loads(raw)
    args, kwargs, _embed = json.loads(base64.b64decode(message["body"]))
    assert (args, kwargs) == ([source, test], {})  # identifiers only: nothing a broker could steer
    assert message["headers"]["task"] == testfetch.TASK_NAME
    assert message["headers"]["expires"]  # a trial that waits too long is dropped, not run late
    assert message["properties"]["delivery_info"] == {"exchange": "", "routing_key": "celery"}


async def test_a_broker_that_does_not_answer_is_unavailable_and_quickly(redis_url: str) -> None:
    dead = CeleryQueue("redis://127.0.0.1:1/0")
    started = time.monotonic()
    with pytest.raises(DependencyUnavailable):
        await dead.send(testfetch.TASK_NAME, [str(uuid.uuid4()), str(uuid.uuid4())], expires=60)
    assert time.monotonic() - started < 15  # a short deadline, not a hang


async def test_a_broker_that_refuses_the_credentials_is_unavailable(redis_url: str) -> None:
    wrong = redis_url.replace("pharos-test-api", "not-the-password")
    assert wrong != redis_url
    with pytest.raises(DependencyUnavailable):
        await CeleryQueue(wrong).send(
            testfetch.TASK_NAME, [str(uuid.uuid4()), str(uuid.uuid4())], expires=60
        )


BINDINGS = "_kombu.binding.celery"
SEPARATOR = "\x06\x16"


@pytest.fixture
def worker(redis_worker_url: str) -> Iterator[Redis]:
    client = Redis.from_url(redis_worker_url)
    yield client
    client.close()


async def test_a_binding_planted_by_the_worker_cannot_make_the_api_write_into_its_own_keys(
    api: Redis, worker: Redis, redis_url: str
) -> None:
    """The table of bindings is writable by the worker's Redis user. Had the message gone through a
    named exchange, kombu would push it onto every queue the table lists, with the API's all-keys
    Redis user. It goes through the anonymous exchange, which never reads the table."""
    from kombu import Connection

    victim = f"pharos:api:testfetch-cooldown:{uuid.uuid4()}"
    planted = f"celery{SEPARATOR}{SEPARATOR}{victim}"
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    assert worker.sadd(BINDINGS, planted) == 1  # the worker may write the table
    try:
        # the premise: through a named exchange, kombu itself would send the message to the victim
        with Connection(redis_url) as connection:
            channel = connection.channel()
            channel.exchange_declare("celery", type="direct")
            assert victim in channel._lookup("celery", "celery")
        # the API's client: one list, and nothing else
        await CeleryQueue(redis_url).send(testfetch.TASK_NAME, ids, expires=60)
        assert api.exists(victim) == 0
        assert api.llen("celery") == 1
    finally:
        api.srem(BINDINGS, planted)
        api.delete(victim)
        while api.rpop("celery"):
            pass


async def test_the_api_never_adds_to_the_table_of_bindings(api: Redis, redis_url: str) -> None:
    before = api.smembers(BINDINGS)
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    await CeleryQueue(redis_url).send(testfetch.TASK_NAME, ids, expires=60)
    try:
        assert api.smembers(BINDINGS) == before
    finally:
        while api.rpop("celery"):
            pass


async def test_a_send_that_takes_too_long_is_unavailable_at_the_deadline(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = CeleryQueue(redis_url, deadline_seconds=0.5)
    monkeypatch.setattr(queue, "_send", lambda *_args: time.sleep(3))
    started = time.monotonic()
    with pytest.raises(DependencyUnavailable):
        await queue.send(testfetch.TASK_NAME, [str(uuid.uuid4()), str(uuid.uuid4())], expires=60)
    assert time.monotonic() - started < 2


async def test_the_send_runs_in_the_queues_own_threads_not_the_default_pool(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    names: list[str] = []
    queue = CeleryQueue(redis_url)
    monkeypatch.setattr(
        queue, "_send", lambda *_args: names.append(threading.current_thread().name)
    )
    await queue.send(testfetch.TASK_NAME, [str(uuid.uuid4()), str(uuid.uuid4())], expires=60)
    assert len(names) == 1 and names[0].startswith("queue-send")
