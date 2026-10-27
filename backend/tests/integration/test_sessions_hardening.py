"""Races, lifetime limits and fail-closed behaviour of the session store (review of M2-03b)."""

import uuid

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.auth import CurrentPrincipal
from pharos.core.config import Settings
from pharos.core.db import create_session_factory
from pharos.core.organization import OrganizationResolver
from pharos.core.sessions import (
    SessionIdentityResolver,
    SessionStore,
    SessionStoreUnavailable,
)
from pharos.main import create_app

from ..clock import FrozenClock
from ..constants import FAKE_SECRET_KEY
from .conftest import Orgs
from .test_request_plumbing import slug_of
from .test_sessions import ABSOLUTE, IDLE, Env, env, ids, session_key  # noqa: F401

pytestmark = pytest.mark.integration


async def first_key(redis: Redis, pattern: str) -> bytes:
    keys = [key async for key in redis.scan_iter(pattern)]
    return keys[0]


@pytest.mark.anyio
async def test_a_new_session_has_the_idle_timeout_from_the_start(env: Env) -> None:  # noqa: F811
    await env.store.create(*ids())
    key = await session_key(env)
    assert 0 < await env.redis.ttl(key) <= IDLE


@pytest.mark.anyio
async def test_a_session_never_read_again_still_expires(env: Env) -> None:  # noqa: F811
    session_id, record = await env.store.create(*ids())
    key = await session_key(env)
    # What Redis does after the idle time: the key is gone, so the cookie is worthless.
    assert await env.redis.ttl(key) > 0
    await env.redis.delete(key)
    assert await env.store.get(session_id) is None
    assert record.created_at == env.clock.now()


@pytest.mark.anyio
async def test_the_first_ttl_never_passes_the_absolute_limit(redis_url: str) -> None:
    redis = Redis.from_url(redis_url.rsplit("/", 1)[0] + "/1")
    prefix = f"test-{uuid.uuid4().hex}:"
    clock = FrozenClock()
    store = SessionStore(redis, clock, idle_seconds=IDLE, absolute_seconds=IDLE // 2, prefix=prefix)
    try:
        await store.create(*ids())
        key = await first_key(redis, f"{prefix}id:*")
        assert 0 < await redis.ttl(key) <= IDLE // 2
    finally:
        await redis.delete(*[k async for k in redis.scan_iter(f"{prefix}*")])
        await redis.aclose()


@pytest.mark.anyio
async def test_a_session_revoked_between_the_read_and_the_touch_is_not_accepted(env: Env) -> None:  # noqa: F811
    session_id, _ = await env.store.create(*ids())
    original = env.redis.get

    async def read_then_lose_the_key(key: str) -> bytes | None:
        value = await original(key)
        await env.redis.delete(key)  # the revoke lands right after the read
        return value

    env.redis.get = read_then_lose_the_key  # type: ignore[method-assign]
    try:
        assert await env.store.get(session_id) is None
    finally:
        env.redis.get = original  # type: ignore[method-assign]


@pytest.mark.anyio
async def test_rotation_cannot_resurrect_a_session_revoked_meanwhile(env: Env) -> None:  # noqa: F811
    user, org = ids()
    old_id, _ = await env.store.create(user, org)
    original_get = env.store.get

    async def get_then_revoke(session_id: str):  # type: ignore[no-untyped-def]
        record = await original_get(session_id)
        await env.store.revoke_user(user)  # "sign out everywhere" lands in the gap
        return record

    env.store.get = get_then_revoke  # type: ignore[method-assign]
    try:
        assert await env.store.rotate(old_id) is None
    finally:
        env.store.get = original_get  # type: ignore[method-assign]
    assert [k async for k in env.redis.scan_iter(f"{env.prefix}id:*")] == []


@pytest.mark.anyio
async def test_a_failed_rotation_leaves_no_second_live_session(env: Env) -> None:  # noqa: F811
    old_id, _ = await env.store.create(*ids())
    rotated = await env.store.rotate(old_id)
    assert rotated is not None
    keys = [k async for k in env.redis.scan_iter(f"{env.prefix}id:*")]
    assert len(keys) == 1


@pytest.mark.anyio
async def test_the_user_index_forgets_sessions_that_have_expired(env: Env) -> None:  # noqa: F811
    user, org = ids()
    for _ in range(5):
        await env.store.create(user, org)
        for key in [k async for k in env.redis.scan_iter(f"{env.prefix}id:*")]:
            await env.redis.delete(key)  # each one expired idle
    await env.store.create(user, org)
    assert await env.redis.scard(f"{env.prefix}user:{user}") == 1


@pytest.mark.anyio
async def test_revoke_counts_only_live_sessions(env: Env) -> None:  # noqa: F811
    user, org = ids()
    await env.store.create(user, org)
    await env.store.create(user, org)
    first = await first_key(env.redis, f"{env.prefix}id:*")
    await env.redis.delete(first)
    assert await env.store.revoke_user(user) == 1


@pytest.mark.anyio
async def test_the_absolute_limit_holds_after_many_reads(env: Env) -> None:  # noqa: F811
    session_id, _ = await env.store.create(*ids())
    # A user who keeps using the session never gets past the absolute limit.
    step = IDLE - 1
    elapsed = 0
    while await env.store.get(session_id) is not None:
        elapsed += step
        assert elapsed < ABSOLUTE + IDLE, "the session outlived the absolute limit"
        env.clock.advance(seconds=step)
    assert elapsed + step >= ABSOLUTE


# --- Redis down --------------------------------------------------------------------------------


def dead_store() -> tuple[SessionStore, Redis]:
    dead = Redis.from_url("redis://127.0.0.1:1/1", socket_connect_timeout=0.2, socket_timeout=0.2)
    return SessionStore(dead, FrozenClock(), idle_seconds=IDLE, absolute_seconds=ABSOLUTE), dead


@pytest.mark.anyio
async def test_malformed_ids_never_reach_redis() -> None:
    store, dead = dead_store()
    try:
        # With Redis down any real lookup would raise; a malformed id must be answered locally.
        for value in ("", "short", "A" * 42, "A" * 44, "../" * 15):
            assert await store.get(value) is None
            await store.delete(value)
            assert await store.rotate(value) is None
    finally:
        await dead.aclose()


@pytest.mark.anyio
async def test_rotation_and_deletion_fail_closed_when_redis_is_down() -> None:
    store, dead = dead_store()
    try:
        with pytest.raises(SessionStoreUnavailable):
            await store.rotate("A" * 43)
        with pytest.raises(SessionStoreUnavailable):
            await store.delete("A" * 43)
    finally:
        await dead.aclose()


@pytest.mark.anyio
async def test_a_signed_in_looking_request_gets_503_when_redis_is_down(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, orgs: Orgs
) -> None:
    store, dead = dead_store()
    app: FastAPI = create_app(Settings(environment="test", secret_key=FAKE_SECRET_KEY))  # type: ignore[arg-type]
    app.state.session_factory = create_session_factory(app_engine)
    app.state.organization = OrganizationResolver(app_engine, await slug_of(owner_engine, orgs[0]))
    app.state.session_store = store
    app.state.sessions = SessionIdentityResolver(store, "pharos_session")

    @app.get("/api/_t/me")
    async def me(principal: CurrentPrincipal) -> dict[str, str]:
        return {"role": principal.role.value}

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/_t/me", headers={"Cookie": f"pharos_session={'A' * 43}"}
            )
        assert response.status_code == 503
        assert response.json()["code"] == "service_unavailable"
    finally:
        await dead.aclose()
