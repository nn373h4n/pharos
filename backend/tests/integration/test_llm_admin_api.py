"""The administrator's page of the model layer: roles, tenancy, what it shows, and "test the key"
(its queue, cooldown, audit and the untrusted answer). The API runs for real against a real Redis
and PostgreSQL; the queue is a recording fake and the worker's answer is written by a client that
connects as the worker's own Redis user, exactly as the worker would."""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from pharos.core.errors import DependencyUnavailable
from pharos.llm import keytest
from pharos.llm.budget import spent_key

from .test_local_auth import (  # noqa: F401  (fixtures and helpers used by these tests)
    MakeStack,
    Stack,
    audit_rows,
    make_stack,
)
from .test_sources_api import _remove_what_the_test_made, details_of  # noqa: F401
from .test_user_admin import person

pytestmark = pytest.mark.integration

SETTINGS: dict[str, Any] = {
    "llm_provider": "fake",
    "llm_model_filter": "small-model",
    "llm_model_enrichment": "big-model",
    "llm_model_insight": "bigger-model",
    "llm_daily_budget_micro_usd": 5_000_000,
}


class FakeQueue:
    def __init__(self) -> None:
        self.sent: list[tuple[str, list[str], int]] = []
        self.fail = False

    async def send(self, name: str, args: list[str], *, expires: int) -> None:
        if self.fail:
            raise DependencyUnavailable
        self.sent.append((name, args, expires))


@pytest.fixture
async def worker_redis(redis_worker_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_worker_url)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def _remove_usage(owner_engine: Any, orgs: Any) -> AsyncIterator[None]:
    yield
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM llm_usage WHERE org_id = ANY(:o)"), {"o": list(orgs)}
        )


async def stack_with_queue(make_stack: MakeStack) -> tuple[Stack, FakeQueue]:
    stack: Stack = await make_stack("on", **SETTINGS)
    queue = FakeQueue()
    stack.app.state.llm_admin._queue = queue
    return stack, queue


def result(test: str, **changes: Any) -> str:
    outcome = changes.get("outcome", "ok")
    checked = datetime.now(UTC).isoformat()
    return f'{{"test_id": "{test}", "outcome": "{outcome}", "checked_at": "{checked}"}}'


async def usage_row(
    stack: Stack, org: uuid.UUID, stage: str, outcome: str, cost: int, **extra: Any
) -> None:
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO llm_usage (org_id, stage, provider, model, tokens_in, tokens_out, "
                "cost_micro_usd, outcome) VALUES (:o, :s, 'fake', 'm', :ti, :to, :c, :out)"
            ),
            {
                "o": org,
                "s": stage,
                "ti": extra.get("tin", 100),
                "to": extra.get("tout", 10),
                "c": cost,
                "out": outcome,
            },
        )


# --- who may call -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_only_administrators_see_the_page_and_ask_for_a_check(make_stack: MakeStack) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    editor = await person(stack, "editor")
    member = await person(stack, "member")
    nobody = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app, client=("10.9.9.9", 1)),
        base_url="http://localhost:3000",
    )
    calls = [
        ("GET", "/api/llm/status"),
        ("POST", "/api/llm/key-test"),
        ("GET", f"/api/llm/key-test/{uuid.uuid4()}"),
    ]
    for method, path in calls:
        assert (await nobody.request(method, path)).status_code in (401, 403)
        for who in (editor, member):
            denied = await who.client.request(method, path, headers=who.headers)
            assert denied.status_code == 403, (who, method, path)
    assert queue.sent == []
    assert (await admin.client.get("/api/llm/status")).status_code == 200
    await nobody.aclose()


@pytest.mark.anyio
async def test_asking_for_a_check_needs_the_csrf_token(make_stack: MakeStack) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    response = await admin.client.post("/api/llm/key-test")
    assert response.status_code == 403 and response.json()["code"] == "csrf_failed"
    assert queue.sent == []


# --- what the page shows ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_page_shows_the_settings_the_spending_and_the_record(
    make_stack: MakeStack, worker_redis: Redis
) -> None:
    stack, _ = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    first = (await admin.client.get("/api/llm/status")).json()
    assert first["provider"] == "fake"
    assert first["models"] == {
        "filter": "small-model",
        "enrichment": "big-model",
        "insight": "bigger-model",
    }
    assert (first["daily_cap_micro_usd"], first["spent_micro_usd"]) == (5_000_000, 0)
    assert (first["warned"], first["exhausted"], first["key_check"], first["usage"]) == (
        False,
        False,
        None,
        [],
    )

    key = spent_key(stack.org_a, datetime.now(UTC).strftime("%Y%m%d"))
    await worker_redis.set(key, "4200000", ex=600)
    await worker_redis.set(f"{key}:warned", "1", ex=600)
    await usage_row(stack, stack.org_a, "enrichment", "ok", 1500, tin=200, tout=40)
    await usage_row(stack, stack.org_a, "enrichment", "ok", 500, tin=100, tout=10)
    await usage_row(stack, stack.org_a, "enrichment", "refused", 0, tin=0, tout=0)
    await usage_row(stack, stack.org_a, "insight", "failed", 70, tin=0, tout=0)
    page = (await admin.client.get("/api/llm/status")).json()
    assert (
        page["spent_micro_usd"] == 4_200_000
        and page["warned"] is True
        and page["exhausted"] is False
    )
    by_stage = {row["stage"]: row for row in page["usage"]}
    assert by_stage["enrichment"] | {"day": None} == {
        "day": None, "stage": "enrichment", "calls": 2, "refused": 1, "failed": 0,
        "tokens_in": 300, "tokens_out": 50, "cost_micro_usd": 2000,
    }  # fmt: skip
    assert (by_stage["insight"]["calls"], by_stage["insight"]["failed"]) == (0, 1)
    await worker_redis.set(f"{key}:exhausted", "1", ex=600)
    assert (await admin.client.get("/api/llm/status")).json()["exhausted"] is True


@pytest.mark.anyio
async def test_an_administrator_never_sees_another_organizations_model_page(
    make_stack: MakeStack, worker_redis: Redis, redis_url: str
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    await usage_row(stack, stack.org_b, "insight", "ok", 99_000, tin=9, tout=9)
    await worker_redis.set(
        spent_key(stack.org_b, datetime.now(UTC).strftime("%Y%m%d")), "777", ex=600
    )
    await worker_redis.set(keytest.last_key(stack.org_b), result(str(uuid.uuid4())), ex=600)
    page = (await admin.client.get("/api/llm/status")).json()
    assert page["usage"] == [] and page["spent_micro_usd"] == 0 and page["key_check"] is None
    # and the check of another organization is not ours: a test started there answers 404 here
    foreign = uuid.uuid4()
    await worker_redis.set(keytest.result_key(foreign), result(str(foreign)), ex=600)
    assert (await admin.client.get(f"/api/llm/key-test/{foreign}")).status_code == 404
    # even with a marker, if it names another organization the answer is the same 404
    api_redis = Redis.from_url(redis_url)
    try:
        await api_redis.set(
            keytest.marker_key(foreign),
            json.dumps({"org": str(stack.org_b), "by": str(uuid.uuid4())}),
            ex=600,
        )
        assert (await admin.client.get(f"/api/llm/key-test/{foreign}")).status_code == 404
        await api_redis.delete(keytest.marker_key(foreign))
    finally:
        await api_redis.aclose()
    assert queue.sent == []


@pytest.mark.anyio
async def test_the_last_check_is_shown_if_it_is_what_the_worker_writes(
    make_stack: MakeStack, worker_redis: Redis
) -> None:
    stack, _ = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    await worker_redis.set(
        keytest.last_key(stack.org_a), result(str(uuid.uuid4()), outcome="rejected"), ex=600
    )
    shown = (await admin.client.get("/api/llm/status")).json()["key_check"]
    assert shown["outcome"] == "rejected" and shown["checked_at"]
    await worker_redis.set(keytest.last_key(stack.org_a), '{"outcome": "sk-ant-LEAK"}', ex=600)
    assert (await admin.client.get("/api/llm/status")).json()["key_check"] is None


# --- "test the key" --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_check_goes_to_the_worker_by_identifier_only_and_is_audited(
    make_stack: MakeStack,
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    started = await admin.client.post("/api/llm/key-test", headers=admin.headers)
    assert started.status_code == 202
    body = started.json()
    assert queue.sent == [(keytest.TASK_NAME, [body["test_id"]], keytest.TASK_EXPIRES_SECONDS)]
    assert body["retry_after_seconds"] == keytest.COOLDOWN_SECONDS
    entries = [e for e in await audit_rows(stack) if e["action"] == "llm.key_test_requested"]
    assert len(entries) == 1 and entries[0]["actor_id"] == admin.id
    assert details_of(entries[0]) == {"provider": "fake"}


@pytest.mark.anyio
async def test_a_second_check_at_once_waits_for_the_cooldown(make_stack: MakeStack) -> None:
    stack, queue = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    assert (await admin.client.post("/api/llm/key-test", headers=admin.headers)).status_code == 202
    again = await admin.client.post("/api/llm/key-test", headers=admin.headers)
    assert (
        again.status_code == 429
        and 0 < int(again.headers["Retry-After"]) <= keytest.COOLDOWN_SECONDS
    )
    assert len(queue.sent) == 1
    other = await person(stack, "admin")  # the cooldown is the organization's, not the person's
    assert (await other.client.post("/api/llm/key-test", headers=other.headers)).status_code == 429


@pytest.mark.anyio
async def test_the_answer_is_pending_then_done_then_read_as_untrusted_data(
    make_stack: MakeStack, worker_redis: Redis
) -> None:
    stack, _ = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    test = (await admin.client.post("/api/llm/key-test", headers=admin.headers)).json()["test_id"]
    read = lambda: admin.client.get(f"/api/llm/key-test/{test}")  # noqa: E731
    assert (await read()).json() == {"status": "pending", "outcome": None, "checked_at": None}
    await worker_redis.set(
        keytest.result_key(uuid.UUID(test)), result(test, outcome="rejected"), ex=60
    )
    done = (await read()).json()
    assert (done["status"], done["outcome"]) == ("done", "rejected") and done["checked_at"]
    for hostile in (
        f'{{"test_id": "{test}", "outcome": "ok", "checked_at": "2026-10-31T12:00:00Z", "key": "sk-ant-LEAK"}}',
        result(str(uuid.uuid4())),  # another test's answer
        f'{{"test_id": "{test}", "outcome": "<script>", "checked_at": "2026-10-31T12:00:00Z"}}',
        "x" * 5000,
        "not json",
    ):
        await worker_redis.set(keytest.result_key(uuid.UUID(test)), hostile, ex=60)
        answer = await read()
        assert answer.json() == {"status": "unavailable", "outcome": None, "checked_at": None}
        assert "LEAK" not in answer.text and "script" not in answer.text


@pytest.mark.anyio
async def test_an_unknown_or_malformed_test_is_a_clean_error(make_stack: MakeStack) -> None:
    stack, _ = await stack_with_queue(make_stack)
    admin = await person(stack, "admin")
    assert (await admin.client.get(f"/api/llm/key-test/{uuid.uuid4()}")).status_code == 404
    assert (await admin.client.get("/api/llm/key-test/not-an-id")).status_code == 422


@pytest.mark.anyio
async def test_when_the_queue_is_down_the_check_fails_cleanly_and_leaves_no_marker(
    make_stack: MakeStack, redis_url: str
) -> None:
    stack, queue = await stack_with_queue(make_stack)
    queue.fail = True
    admin = await person(stack, "admin")
    client = Redis.from_url(redis_url)
    try:
        before = {k async for k in client.scan_iter(match="pharos:api:llm-keytest:*")}
        response = await admin.client.post("/api/llm/key-test", headers=admin.headers)
        assert response.status_code == 503
        after = {k async for k in client.scan_iter(match="pharos:api:llm-keytest:*")}
        assert after - before == set()
        assert await client.exists(keytest.cooldown_key(stack.org_a)) == 1  # a retry waits
    finally:
        await client.aclose()
