"""The test fetch through the API: roles, tenancy, cooldown, queue, audit, and the untrusted answer.

The API runs for real against a real Redis. The queue is a recording fake (what the real one sends is
checked in test_task_queue.py), and the worker's answer is written by a second Redis client that
connects as the worker's own Redis user, exactly as the worker would.
"""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from pharos.core.errors import DependencyUnavailable
from pharos.ingest import testfetch

from .test_local_auth import (  # noqa: F401  (fixtures and helpers used by these tests)
    PUBLIC_URL,
    MakeStack,
    Stack,
    audit_rows,
    make_stack,
)
from .test_sources_api import (  # noqa: F401  (the cleaning fixture applies to this module too)
    FEED,
    _remove_what_the_test_made,
    details_of,
    foreign_source,
    post,
    rows,
)
from .test_user_admin import Person, person

pytestmark = pytest.mark.integration


class FakeQueue:
    def __init__(self) -> None:
        self.sent: list[tuple[str, list[str], int]] = []
        self.fail = False

    async def send(self, name: str, args: list[str], *, expires: int) -> None:
        if self.fail:
            raise DependencyUnavailable
        self.sent.append((name, args, expires))


class BrokenRedis:
    """Every command fails the way a Redis that is down does."""

    def __getattr__(self, name: str) -> Any:
        async def command(*_: Any, **__: Any) -> Any:
            raise RedisConnectionError("down")

        return command


@pytest.fixture
async def worker_redis(redis_worker_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_worker_url)
    yield client
    await client.aclose()


@pytest.fixture
async def api_redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    yield client
    await client.aclose()


async def keys_of(client: Redis) -> list[bytes]:
    """What the API has left under its test-fetch prefix (SCAN: the API user may not use KEYS)."""
    return [key async for key in client.scan_iter(match="pharos:api:testfetch*")]


async def stack_with_queue(make_stack: MakeStack) -> tuple[Stack, FakeQueue]:
    stack: Stack = await make_stack("on")
    queue = FakeQueue()
    stack.app.state.sources._queue = queue
    return stack, queue


async def start(who: Person, source: str | uuid.UUID) -> httpx.Response:
    return await who.client.post(f"/api/sources/{source}/test-fetch", headers=who.headers)


async def answer(who: Person, source: str | uuid.UUID, test: str | uuid.UUID) -> httpx.Response:
    return await who.client.get(f"/api/sources/{source}/test-fetch/{test}")


def good_result(test: str, source: str, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "v": 1,
        "test_id": test,
        "source_id": source,
        "outcome": "ok",
        "entries_seen": 3,
        "entries_skipped": 0,
        "bytes_fetched": 2048,
        "sample": [{"title": "First", "url": "https://news.example.org/1", "published_at": None}],
    }
    body.update(changes)
    return body


async def made_source(admin: Person, **changes: Any) -> str:
    return (await post(admin, {**FEED, **changes})).json()["id"]


# --- who may call -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_only_administrators_may_start_or_read_a_trial_fetch(make_stack: MakeStack) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    editor = await person(stack, "editor")
    member = await person(stack, "member")
    nobody = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app, client=("10.1.2.3", 4000)),
        base_url=PUBLIC_URL,
        headers={"Origin": PUBLIC_URL},
    )
    try:
        source = await made_source(admin)
        paths = [
            ("POST", f"/api/sources/{source}/test-fetch"),
            ("GET", f"/api/sources/{source}/test-fetch/{uuid.uuid4()}"),
        ]
        for method, path in paths:
            assert (await nobody.request(method, path)).status_code == 401
            for who in (editor, member):
                denied = await who.client.request(method, path, headers=who.headers)
                assert denied.status_code == 403 and denied.json()["code"] == "forbidden"
        assert queue.sent == []  # nobody but an administrator got anything started
        assert (await start(admin, source)).status_code == 202
    finally:
        await nobody.aclose()


@pytest.mark.anyio
async def test_starting_a_trial_fetch_needs_the_csrf_token(make_stack: MakeStack) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    source = await made_source(admin)
    response = await admin.client.post(f"/api/sources/{source}/test-fetch")
    assert response.status_code == 403 and response.json()["code"] == "csrf_failed"
    assert queue.sent == []


# --- starting ----------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_trial_fetch_hands_the_worker_two_identifiers_and_nothing_else(
    make_stack: MakeStack, api_redis: Redis
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    source = await made_source(admin)
    response = await start(admin, source)
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"test_id", "retry_after_seconds"} and body["retry_after_seconds"] == 30
    [(name, args, expires)] = queue.sent
    assert (name, args, expires) == ("pharos.ingest.test_fetch", [source, body["test_id"]], 60)
    assert "news.example.org" not in json.dumps(queue.sent)  # no address travels through the broker
    # the marker lives where the worker's Redis user cannot reach, and says whose test it is
    marker = json.loads(await api_redis.get(f"pharos:api:testfetch:{body['test_id']}"))
    assert marker == {"org": str(stack.org_a), "source": source, "by": str(admin.id)}
    assert 0 < await api_redis.ttl(f"pharos:api:testfetch:{body['test_id']}") <= 300
    assert 0 < await api_redis.ttl(f"pharos:api:testfetch-cooldown:{stack.org_a}:{source}") <= 30


@pytest.mark.anyio
async def test_a_trial_fetch_is_audited_without_the_address(make_stack: MakeStack) -> None:
    stack, _ = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    source = await made_source(admin)
    assert (await start(admin, source)).status_code == 202
    [entry] = [e for e in await audit_rows(stack) if e["action"] == "source.test_fetch_requested"]
    assert (entry["outcome"], entry["target"], entry["actor_id"]) == (
        "success",
        f"source:{source}",
        admin.id,
    )
    assert details_of(entry) == {"type": "rss"}
    assert "news.example" not in json.dumps(entry, default=str)


@pytest.mark.anyio
async def test_a_source_that_is_not_enabled_yet_can_be_tried(make_stack: MakeStack) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    source = await made_source(admin, enabled=False)
    assert (await start(admin, source)).status_code == 202 and len(queue.sent) == 1


@pytest.mark.anyio
async def test_a_second_trial_of_the_same_source_waits_for_the_cooldown(
    make_stack: MakeStack, api_redis: Redis
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    first = await made_source(admin)
    other = await made_source(admin, name="Other", config={"url": "https://other.example.org/f"})
    assert (await start(admin, first)).status_code == 202
    again = await start(admin, first)
    assert again.status_code == 429 and again.json()["code"] == "rate_limited"
    assert 1 <= int(again.headers["Retry-After"]) <= 30
    assert len(queue.sent) == 1  # the second one never reached the worker
    assert (
        len([e for e in await audit_rows(stack) if e["action"] == "source.test_fetch_requested"])
        == 1
    )
    assert (await start(admin, other)).status_code == 202  # another source has its own cooldown
    await api_redis.delete(f"pharos:api:testfetch-cooldown:{stack.org_a}:{first}")
    assert (await start(admin, first)).status_code == 202  # and a cooldown that ran out is over
    assert len(queue.sent) == 3


@pytest.mark.anyio
async def test_a_source_of_another_organization_or_a_personal_one_is_not_found(
    make_stack: MakeStack, api_redis: Redis
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    before = set(await keys_of(api_redis))  # other tests share this Redis
    owner = await person(stack, "member")
    theirs = await foreign_source(stack)
    personal = await foreign_source(stack, org_id=stack.org_a, owner_user_id=owner.id)
    for source in (theirs, personal, uuid.uuid4()):
        response = await start(admin, source)
        assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert (await start(admin, "not-a-uuid")).status_code == 422
    assert queue.sent == [] and set(await keys_of(api_redis)) == before


@pytest.mark.anyio
async def test_a_stored_address_that_is_internal_now_is_refused_and_audited(
    make_stack: MakeStack, api_redis: Redis
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    old = await foreign_source(stack, org_id=stack.org_a, config='{"url": "http://10.0.0.5/feed"}')
    response = await start(admin, old)
    assert response.status_code == 422 and response.json()["errors"][0]["type"] == "url_not_allowed"
    assert queue.sent == []
    assert await api_redis.exists(f"pharos:api:testfetch-cooldown:{stack.org_a}:{old}") == 0
    [entry] = [e for e in await audit_rows(stack) if e["action"] == "source.test_fetch_requested"]
    assert entry["outcome"] == "failure" and "10.0.0.5" not in json.dumps(entry, default=str)


# --- when something is down ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_broker_that_is_down_is_a_503_and_keeps_the_cooldown_but_not_the_marker(
    make_stack: MakeStack, api_redis: Redis
) -> None:
    """The task may have reached the broker even though we did not hear: a retry at once could then
    fetch the source twice, so the person waits out the cooldown. The marker goes."""
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    before = set(await keys_of(api_redis))  # other tests share this Redis
    source = await made_source(admin)
    queue.fail = True
    response = await start(admin, source)
    assert response.status_code == 503 and response.json()["code"] == "service_unavailable"
    left = set(await keys_of(api_redis)) - before
    assert {k.decode() for k in left} == {
        f"pharos:api:testfetch-cooldown:{stack.org_a}:{source}",
        f"pharos:api:testfetch-rate:{stack.org_a}",
    }  # no marker is left
    events = [e for e in await audit_rows(stack) if e["action"] == "source.test_fetch_requested"]
    assert [e["outcome"] for e in events] == ["success", "failure"]
    assert details_of(events[1])["reason"] == "unavailable"
    queue.fail = False
    assert (await start(admin, source)).status_code == 429  # wait out the cooldown first
    await api_redis.delete(f"pharos:api:testfetch-cooldown:{stack.org_a}:{source}")
    assert (await start(admin, source)).status_code == 202


@pytest.mark.anyio
async def test_an_organization_may_start_only_a_few_trials_a_minute_whatever_the_sources(
    make_stack: MakeStack, api_redis: Redis
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admins = [await person(stack, "admin") for _ in range(3)]
    sources = [
        await made_source(admins[0], name=f"S{n}", config={"url": f"https://f{n}.example.org/x"})
        for n in range(testfetch.ORG_TESTS_PER_MINUTE + 2)
    ]
    answers = [await start(admins[n % 3], source) for n, source in enumerate(sources)]
    codes = [a.status_code for a in answers]
    assert codes == [202] * testfetch.ORG_TESTS_PER_MINUTE + [429, 429]
    assert len(queue.sent) == testfetch.ORG_TESTS_PER_MINUTE
    assert 1 <= int(answers[-1].headers["Retry-After"]) <= testfetch.RATE_WINDOW_SECONDS
    assert answers[-1].json()["code"] == "rate_limited"
    # a trial that did not start leaves no cooldown behind: it can go as soon as the window is over
    for source in sources[-2:]:
        assert await api_redis.exists(f"pharos:api:testfetch-cooldown:{stack.org_a}:{source}") == 0
    await api_redis.delete(f"pharos:api:testfetch-rate:{stack.org_a}")
    assert (await start(admins[0], sources[-1])).status_code == 202


@pytest.mark.anyio
async def test_a_redis_that_is_down_is_a_503_for_both_calls(make_stack: MakeStack) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    source = await made_source(admin)
    stack.app.state.sources._redis = BrokenRedis()
    started = await start(admin, source)
    assert started.status_code == 503 and queue.sent == []
    read = await answer(admin, source, uuid.uuid4())
    assert read.status_code == 503


# --- reading the answer --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_answer_is_pending_until_the_worker_has_left_one(
    make_stack: MakeStack, worker_redis: Redis
) -> None:
    stack, _ = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    source = await made_source(admin)
    test = (await start(admin, source)).json()["test_id"]
    pending = await answer(admin, source, test)
    assert pending.status_code == 200 and pending.json()["status"] == "pending"
    assert pending.json()["sample"] == [] and pending.json()["outcome"] is None
    await worker_redis.set(
        f"pharos:worker:testfetch:{test}", json.dumps(good_result(test, source)), ex=120
    )
    done = (await answer(admin, source, test)).json()
    assert (done["status"], done["outcome"], done["entries_seen"], done["bytes_fetched"]) == (
        "done",
        "ok",
        3,
        2048,
    )
    assert done["sample"] == [
        {"title": "First", "url": "https://news.example.org/1", "published_at": None}
    ]


@pytest.mark.anyio
async def test_a_failed_trial_shows_fixed_words_only(
    make_stack: MakeStack, worker_redis: Redis
) -> None:
    stack, _ = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    source = await made_source(admin)
    test = (await start(admin, source)).json()["test_id"]
    failure = good_result(
        test,
        source,
        outcome="bad_status",
        error_code="http_error",
        http_status=404,
        sample=[],
        entries_seen=0,
    )
    await worker_redis.set(f"pharos:worker:testfetch:{test}", json.dumps(failure), ex=120)
    shown = (await answer(admin, source, test)).json()
    assert (shown["status"], shown["outcome"], shown["error_code"], shown["http_status"]) == (
        "done",
        "bad_status",
        "http_error",
        404,
    )


FORGERIES = {
    "not json": b"<html>nope</html>",
    "a list": b"[]",
    "an extra field": json.dumps({"extra": "<script>alert(1)</script>"}).encode(),
}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        {"outcome": "pwned"},
        {"extra": "<script>alert(1)</script>"},
        {"sample": [{"title": "t", "url": "javascript:alert(1)"}]},
        {"sample": [{"title": "t", "url": "https://a.example/"}] * 6},
        {"entries_seen": "3"},
        {"http_status": 99},
        {"error_code": "<b>x</b>"},
        {"test_id": str(uuid.uuid4())},
        {"source_id": str(uuid.uuid4())},
        {"v": 2},
    ],
)
async def test_an_answer_that_is_not_what_the_protocol_allows_is_never_shown(
    make_stack: MakeStack, worker_redis: Redis, change: dict[str, Any]
) -> None:
    stack, _ = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    source = await made_source(admin)
    test = (await start(admin, source)).json()["test_id"]
    await worker_redis.set(
        f"pharos:worker:testfetch:{test}", json.dumps(good_result(test, source, **change)), ex=120
    )
    shown = await answer(admin, source, test)
    assert shown.status_code == 200
    assert shown.json() == {
        "status": "unavailable", "outcome": None, "error_code": None, "http_status": None,
        "entries_seen": None, "entries_skipped": None, "bytes_fetched": None, "sample": [],
    }  # fmt: skip
    assert "script" not in shown.text and "javascript" not in shown.text


@pytest.mark.anyio
@pytest.mark.parametrize("raw", [*FORGERIES.values(), b"x" * 20_000, b"\xff\xfe\x00"], ids=range(5))
async def test_garbage_left_by_the_worker_is_unavailable_and_not_echoed(
    make_stack: MakeStack, worker_redis: Redis, raw: bytes
) -> None:
    stack, _ = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    source = await made_source(admin)
    test = (await start(admin, source)).json()["test_id"]
    await worker_redis.set(f"pharos:worker:testfetch:{test}", raw, ex=120)
    shown = await answer(admin, source, test)
    assert shown.status_code == 200 and shown.json()["status"] == "unavailable"
    assert "<html>" not in shown.text and "nope" not in shown.text and "alert" not in shown.text


@pytest.mark.anyio
async def test_an_answer_for_a_test_nobody_asked_for_here_is_not_found(
    make_stack: MakeStack, worker_redis: Redis, api_redis: Redis
) -> None:
    stack, _ = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    source = await made_source(admin)
    other = await made_source(admin, name="Other", config={"url": "https://other.example.org/f"})
    # the worker (or someone with its key) leaves an answer for an id that no administrator started
    stray = str(uuid.uuid4())
    await worker_redis.set(
        f"pharos:worker:testfetch:{stray}", json.dumps(good_result(stray, source)), ex=120
    )
    assert (await answer(admin, source, stray)).status_code == 404
    # a test of one source is not readable under another source
    test = (await start(admin, source)).json()["test_id"]
    assert (await answer(admin, other, test)).status_code == 404
    assert (await answer(admin, source, test)).status_code == 200
    # a marker of another organization's test is not ours
    foreign = str(uuid.uuid4())
    marker = {"org": str(stack.org_b), "source": source, "by": str(admin.id)}
    await api_redis.set(f"pharos:api:testfetch:{foreign}", json.dumps(marker), ex=60)
    assert (await answer(admin, source, foreign)).status_code == 404
    # a marker that is not what the API wrote
    broken = str(uuid.uuid4())
    await api_redis.set(f"pharos:api:testfetch:{broken}", b"not json", ex=60)
    assert (await answer(admin, source, broken)).status_code == 404
    # an expired test
    await api_redis.delete(f"pharos:api:testfetch:{test}")
    assert (await answer(admin, source, test)).status_code == 404
    assert (await answer(admin, source, "not-a-uuid")).status_code == 422


@pytest.mark.anyio
async def test_the_answer_of_a_source_of_another_organization_is_not_found(
    make_stack: MakeStack,
) -> None:
    stack, _ = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    theirs = await foreign_source(stack)
    assert (await answer(admin, theirs, uuid.uuid4())).status_code == 404
    assert (await rows(stack, "SELECT 1 FROM sources WHERE id = :i", i=theirs)) != []


@pytest.mark.anyio
async def test_the_application_is_wired_with_the_real_queue_and_the_brokers_redis(
    make_stack: MakeStack,
) -> None:
    from pharos.core.queue import CeleryQueue

    stack: Stack = await make_stack("on")
    service = stack.app.state.sources
    assert isinstance(service._queue, CeleryQueue)
    assert isinstance(service._redis, Redis)
    # the same Redis database the worker's Celery uses, not the sessions' one
    assert service._redis.connection_pool.connection_kwargs.get("db", 0) == 0
