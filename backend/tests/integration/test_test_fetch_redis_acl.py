"""The two sides of the test fetch cannot reach each other's keys (ADR-0013).

The API owns the markers and the cooldowns (`pharos:api:testfetch*`); the worker owns its answers
(`pharos:worker:testfetch:*`). Each refusal has a positive control, so it is the rules at work and
not a broken connection.
"""

import json
import uuid
from collections.abc import Iterator

import pytest
from redis import Redis
from redis.exceptions import NoPermissionError

from pharos.ingest import testfetch

pytestmark = pytest.mark.integration

TEST = uuid.uuid4()
SOURCE = uuid.uuid4()
ORG = uuid.uuid4()
API_KEYS = [testfetch.marker_key(TEST), testfetch.cooldown_key(ORG, SOURCE)]


@pytest.fixture
def api(redis_url: str) -> Iterator[Redis]:
    client = Redis.from_url(redis_url)
    yield client
    client.delete(*API_KEYS, testfetch.result_key(TEST))
    client.close()


@pytest.fixture
def worker(redis_worker_url: str) -> Iterator[Redis]:
    client = Redis.from_url(redis_worker_url)
    yield client
    client.close()


def test_the_worker_can_leave_its_answer_and_the_api_can_read_it(api: Redis, worker: Redis) -> None:
    key = testfetch.result_key(TEST)
    assert worker.set(key, '{"v": 1}', ex=testfetch.RESULT_TTL_SECONDS)
    assert api.get(key) == b'{"v": 1}'
    assert 0 < api.ttl(key) <= testfetch.RESULT_TTL_SECONDS
    # the answer is the worker's alone: the API's namespace is not where it lives
    assert not key.startswith(testfetch.MARKER_PREFIX)


@pytest.mark.parametrize("key", API_KEYS)
def test_the_worker_cannot_forge_a_marker_or_lift_a_cooldown(
    api: Redis, worker: Redis, key: str
) -> None:
    assert api.set(key, "ok", ex=60)  # positive control: the API may
    with pytest.raises(NoPermissionError):
        worker.set(key, "forged", ex=60)
    with pytest.raises(NoPermissionError):
        worker.delete(key)
    with pytest.raises(NoPermissionError):
        worker.expire(key, 1)
    assert api.get(key) == b"ok"


@pytest.mark.parametrize("key", API_KEYS)
def test_the_worker_cannot_read_what_the_api_recorded(api: Redis, worker: Redis, key: str) -> None:
    marker = json.dumps({"org": str(ORG), "source": str(SOURCE), "by": str(uuid.uuid4())})
    api.set(key, marker, ex=60)
    with pytest.raises(NoPermissionError):
        worker.get(key)
    with pytest.raises(NoPermissionError):
        worker.ttl(key)
    with pytest.raises(NoPermissionError):
        worker.exists(key)


def test_the_worker_cannot_reach_a_marker_through_a_script(api: Redis, worker: Redis) -> None:
    key = testfetch.marker_key(TEST)
    api.set(key, "ok", ex=60)
    script = "return redis.call('GET', KEYS[1])"
    assert api.eval(script, 1, key) == b"ok"  # positive control
    with pytest.raises(NoPermissionError):
        worker.eval(script, 1, key)


def test_the_worker_cannot_write_outside_its_prefix_by_a_look_alike_name(worker: Redis) -> None:
    for key in (
        "pharos:workerx:testfetch:a",
        "pharos:api:testfetch:a",
        "pharos:worker",
        "pharos:session:x",
    ):
        with pytest.raises(NoPermissionError):
            worker.set(key, "x")
