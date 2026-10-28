"""The session store against real Redis, and the cookie-to-principal path against real PostgreSQL."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.auth import CurrentPrincipal
from pharos.core.config import Settings
from pharos.core.csrf import HOST_PREFIXED_SESSION_COOKIE, csrf_token
from pharos.core.db import create_session_factory
from pharos.core.organization import OrganizationResolver
from pharos.core.redis import url_for_database
from pharos.core.secrets import KeyPurpose, derive_key
from pharos.core.sessions import (
    SessionIdentityResolver,
    SessionStore,
    SessionStoreUnavailable,
    is_well_formed,
)
from pharos.main import create_app

from ..clock import FrozenClock
from ..constants import FAKE_SECRET_KEY
from .conftest import Orgs
from .test_request_plumbing import slug_of

pytestmark = pytest.mark.integration

IDLE, ABSOLUTE = 600, 3600
PUBLIC_URL = "https://pharos.example"


@dataclass
class Env:
    store: SessionStore
    redis: Redis
    clock: FrozenClock
    prefix: str


@pytest.fixture
async def env(redis_url: str) -> AsyncIterator[Env]:
    redis = Redis.from_url(url_for_database(redis_url, 1))
    clock = FrozenClock()
    prefix = f"test-{uuid.uuid4().hex}:"
    store = SessionStore(redis, clock, idle_seconds=IDLE, absolute_seconds=ABSOLUTE, prefix=prefix)
    yield Env(store, redis, clock, prefix)
    keys = [key async for key in redis.scan_iter(f"{prefix}*")]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()


async def session_key(env: Env) -> bytes:
    keys = [key async for key in env.redis.scan_iter(f"{env.prefix}id:*")]
    assert len(keys) == 1
    return keys[0]


def ids() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


@pytest.mark.anyio
async def test_a_created_session_can_be_read_back(env: Env) -> None:
    user, org = ids()
    session_id, record = await env.store.create(user, org)
    assert len(session_id) == 43
    found = await env.store.get(session_id)
    assert found == record
    assert (found.user_id, found.org_id) == (user, org)


@pytest.mark.anyio
async def test_session_ids_are_distinct_and_well_formed(env: Env) -> None:
    user, org = ids()
    created = {(await env.store.create(user, org))[0] for _ in range(50)}
    assert len(created) == 50
    assert all(is_well_formed(session_id) for session_id in created)


@pytest.mark.anyio
async def test_redis_never_holds_the_cookie_value(env: Env) -> None:
    session_id, _ = await env.store.create(*ids())
    keys = [key.decode() async for key in env.redis.scan_iter(f"{env.prefix}*")]
    assert keys
    assert not any(session_id in key for key in keys)
    for key in keys:
        kind = await env.redis.type(key)
        if kind == b"string":
            assert session_id.encode() not in (await env.redis.get(key) or b"")
        else:
            members = await env.redis.smembers(key)
            assert all(session_id.encode() not in member for member in members)


@pytest.mark.anyio
async def test_an_unknown_id_is_not_a_session(env: Env) -> None:
    assert await env.store.get("A" * 43) is None


@pytest.mark.anyio
async def test_the_idle_timeout_slides_but_the_absolute_limit_does_not(env: Env) -> None:
    session_id, _ = await env.store.create(*ids())
    key = await session_key(env)

    await env.redis.expire(key, 5)  # nearly idle
    assert await env.store.get(session_id) is not None
    assert 5 < await env.redis.ttl(key) <= IDLE  # extended by the read

    env.clock.advance(seconds=ABSOLUTE - 100)
    assert await env.store.get(session_id) is not None
    assert await env.redis.ttl(key) <= 100  # never beyond the absolute limit

    env.clock.advance(seconds=100)
    assert await env.store.get(session_id) is None
    assert await env.redis.exists(key) == 0


@pytest.mark.anyio
async def test_redis_expiry_ends_an_idle_session(env: Env) -> None:
    session_id, _ = await env.store.create(*ids())
    key = await session_key(env)
    await env.redis.delete(key)  # what the idle expiry does
    assert await env.store.get(session_id) is None


@pytest.mark.anyio
async def test_a_corrupt_record_is_treated_as_no_session_and_removed(env: Env) -> None:
    session_id, _ = await env.store.create(*ids())
    key = await session_key(env)
    await env.redis.set(key, "not json")
    assert await env.store.get(session_id) is None
    assert await env.redis.exists(key) == 0


@pytest.mark.anyio
async def test_delete_ends_the_session(env: Env) -> None:
    user, org = ids()
    session_id, _ = await env.store.create(user, org)
    await env.store.delete(session_id)
    assert await env.store.get(session_id) is None
    assert await env.redis.scard(f"{env.prefix}user:{user}") == 0


@pytest.mark.anyio
async def test_rotation_replaces_the_id_and_the_csrf_seed(env: Env) -> None:
    user, org = ids()
    old_id, old = await env.store.create(user, org)
    rotated = await env.store.rotate(old_id)
    assert rotated is not None
    new_id, new = rotated
    assert new_id != old_id
    assert await env.store.get(old_id) is None
    assert await env.store.get(new_id) == new
    assert new.csrf_seed != old.csrf_seed
    assert (new.user_id, new.org_id, new.created_at) == (user, org, old.created_at)
    assert await env.store.rotate("A" * 43) is None


@pytest.mark.anyio
async def test_rotation_does_not_extend_the_absolute_lifetime(env: Env) -> None:
    old_id, _ = await env.store.create(*ids())
    env.clock.advance(seconds=ABSOLUTE - 10)
    rotated = await env.store.rotate(old_id)
    assert rotated is not None
    env.clock.advance(seconds=10)
    assert await env.store.get(rotated[0]) is None


@pytest.mark.anyio
async def test_revoking_a_user_deletes_all_their_sessions_and_only_theirs(env: Env) -> None:
    user, org = ids()
    other, _ = ids()
    mine = [(await env.store.create(user, org))[0] for _ in range(3)]
    theirs, _ = await env.store.create(other, org)
    assert await env.store.revoke_user(user) == 3
    assert [await env.store.get(session_id) for session_id in mine] == [None] * 3
    assert await env.store.get(theirs) is not None
    assert await env.store.revoke_user(user) == 0


@pytest.mark.anyio
async def test_when_redis_is_down_every_operation_fails_closed(redis_url: str) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/1", socket_connect_timeout=0.2, socket_timeout=0.2)
    store = SessionStore(dead, FrozenClock(), idle_seconds=IDLE, absolute_seconds=ABSOLUTE)
    try:
        for call in (
            store.create(*ids()),
            store.get("A" * 43),
            store.delete("A" * 43),
            store.revoke_user(uuid.uuid4()),
        ):
            with pytest.raises(SessionStoreUnavailable):
                await call
    finally:
        await dead.aclose()


@pytest.mark.anyio
async def test_sessions_live_in_their_own_database(env: Env, redis_url: str) -> None:
    await env.store.create(*ids())
    default = Redis.from_url(url_for_database(redis_url, 0))
    try:
        assert [k async for k in default.scan_iter(f"{env.prefix}*")] == []
    finally:
        await default.aclose()


# --- cookie to principal -----------------------------------------------------------------------


@dataclass
class Stack:
    client: httpx.AsyncClient
    env: Env
    owner: AsyncEngine
    org_a: uuid.UUID
    org_b: uuid.UUID


@pytest.fixture
async def stack(
    env: Env, app_engine: AsyncEngine, owner_engine: AsyncEngine, orgs: Orgs
) -> AsyncIterator[Stack]:
    org_a, org_b = orgs
    app = create_app(
        Settings(
            environment="test", public_url=PUBLIC_URL, secret_key=FAKE_SECRET_KEY, local_login="on"
        )  # type: ignore[arg-type]
    )
    app.state.session_factory = create_session_factory(app_engine)
    app.state.organization = OrganizationResolver(app_engine, await slug_of(owner_engine, org_a))
    app.state.session_store = env.store
    app.state.sessions = SessionIdentityResolver(env.store, HOST_PREFIXED_SESSION_COOKIE)

    @app.get("/api/_t/me")
    async def me(principal: CurrentPrincipal) -> dict[str, str]:
        return {"role": principal.role.value}

    @app.post("/api/_t/change")
    async def change(principal: CurrentPrincipal) -> dict[str, str]:
        return {"user": str(principal.user_id)}

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Stack(client, env, owner_engine, org_a, org_b)
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM users WHERE org_id = ANY(:ids)"), {"ids": [org_a, org_b]}
        )


async def sign_in(
    stack: Stack, *, org: uuid.UUID | None = None, status: str = "active"
) -> tuple[uuid.UUID, str, dict[str, str]]:
    org = org or stack.org_a
    async with stack.owner.begin() as connection:
        row = await connection.execute(
            text(
                "INSERT INTO users (org_id, issuer, subject, role, status) "
                "VALUES (:org, 'local', :sub, 'admin', :status) RETURNING id"
            ),
            {"org": org, "sub": uuid.uuid4().hex, "status": status},
        )
        user = row.scalar_one()
    session_id, record = await stack.env.store.create(user, org)
    token = csrf_token(derive_key(FAKE_SECRET_KEY_SECRET, KeyPurpose.CSRF), session_id, record)
    return (
        user,
        session_id,
        {
            "Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={session_id}",
            "X-CSRF-Token": token,
            "Origin": PUBLIC_URL,
        },
    )


FAKE_SECRET_KEY_SECRET = Settings(environment="test", secret_key=FAKE_SECRET_KEY).secret_key  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_a_session_cookie_signs_the_user_in(stack: Stack) -> None:
    _, _, headers = await sign_in(stack)
    response = await stack.client.get("/api/_t/me", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"role": "admin"}
    assert (await stack.client.get("/api/_t/me")).status_code == 401


@pytest.mark.anyio
async def test_a_forged_or_foreign_cookie_is_refused(stack: Stack) -> None:
    for value in ("A" * 43, "garbage", ""):
        response = await stack.client.get(
            "/api/_t/me", headers={"Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={value}"}
        )
        assert response.status_code == 401
    # The same value under the name without the prefix is not read at all.
    _, session_id, _ = await sign_in(stack)
    unprefixed = await stack.client.get(
        "/api/_t/me", headers={"Cookie": f"pharos_session={session_id}"}
    )
    assert unprefixed.status_code == 401


@pytest.mark.anyio
async def test_revoking_the_user_signs_them_out_on_the_next_request(stack: Stack) -> None:
    user, _, headers = await sign_in(stack)
    assert (await stack.client.get("/api/_t/me", headers=headers)).status_code == 200
    await stack.env.store.revoke_user(user)
    assert (await stack.client.get("/api/_t/me", headers=headers)).status_code == 401


@pytest.mark.anyio
async def test_disabling_the_user_blocks_a_live_session_without_touching_redis(
    stack: Stack,
) -> None:
    user, session_id, headers = await sign_in(stack)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE users SET status = 'disabled' WHERE id = :id"), {"id": user}
        )
    assert await stack.env.store.get(session_id) is not None  # the session itself is still there
    assert (await stack.client.get("/api/_t/me", headers=headers)).status_code == 401


@pytest.mark.anyio
async def test_a_state_changing_request_needs_origin_and_token_end_to_end(stack: Stack) -> None:
    _, _, headers = await sign_in(stack)
    ok = await stack.client.post("/api/_t/change", headers=headers)
    no_token = await stack.client.post(
        "/api/_t/change", headers={k: v for k, v in headers.items() if k != "X-CSRF-Token"}
    )
    no_origin = await stack.client.post(
        "/api/_t/change", headers={k: v for k, v in headers.items() if k != "Origin"}
    )
    assert (ok.status_code, no_token.status_code, no_origin.status_code) == (200, 403, 403)


@pytest.mark.anyio
async def test_a_rotated_session_invalidates_the_old_cookie_and_token(stack: Stack) -> None:
    _, session_id, headers = await sign_in(stack)
    rotated = await stack.env.store.rotate(session_id)
    assert rotated is not None
    assert (await stack.client.get("/api/_t/me", headers=headers)).status_code == 401
    new_id, record = rotated
    key = derive_key(FAKE_SECRET_KEY_SECRET, KeyPurpose.CSRF)
    fresh = {
        "Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={new_id}",
        "X-CSRF-Token": csrf_token(key, new_id, record),
        "Origin": PUBLIC_URL,
    }
    assert (await stack.client.post("/api/_t/change", headers=fresh)).status_code == 200
    # The token that belonged to the old id and seed is useless with the new cookie.
    stale = {**fresh, "X-CSRF-Token": headers["X-CSRF-Token"]}
    assert (await stack.client.post("/api/_t/change", headers=stale)).status_code == 403


@pytest.mark.anyio
async def test_a_session_for_another_organization_is_refused(stack: Stack) -> None:
    _, _, headers = await sign_in(stack, org=stack.org_b)
    assert (await stack.client.get("/api/_t/me", headers=headers)).status_code == 401
