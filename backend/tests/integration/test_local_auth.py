"""Local sign-in, sign-out and /api/me through the real application."""

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos import cli
from pharos.core.config import Settings
from pharos.core.csrf import HOST_PREFIXED_SESSION_COOKIE
from pharos.main import create_app

from ..constants import FAKE_SECRET_KEY
from .conftest import Orgs, TestDatabase
from .test_request_plumbing import slug_of

pytestmark = pytest.mark.integration

PUBLIC_URL = "https://pharos.example"
PASSWORD = "correct horse battery staple"
OTHER_PASSWORD = "another long passphrase here"
GENERIC = {"invalid_credentials"}


@dataclass
class Stack:
    client: httpx.AsyncClient
    app: FastAPI
    settings: Settings
    owner: AsyncEngine
    org_a: uuid.UUID
    org_b: uuid.UUID
    verify_calls: list[bool]


MakeStack = Callable[..., Any]


@pytest.fixture
async def make_stack(
    database: TestDatabase, redis_url: str, owner_engine: AsyncEngine, orgs: Orgs
) -> AsyncIterator[MakeStack]:
    contexts: list[Any] = []

    async def build(mode: str = "break-glass", **extra: Any) -> Stack:
        org_a, org_b = orgs
        settings = Settings(
            environment="test",
            database_url=database.app_url,  # type: ignore[arg-type]
            redis_url=redis_url,  # type: ignore[arg-type]
            public_url=PUBLIC_URL,
            secret_key=FAKE_SECRET_KEY,  # type: ignore[arg-type]
            org_slug=await slug_of(owner_engine, org_a),
            local_login=mode,  # type: ignore[arg-type]
            password_hash_memory_kib=64,
            password_hash_time_cost=1,
            **extra,
        )
        app = create_app(settings)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        # Each stack gets its own client address, so the per-address limit does not leak between tests.
        octets = uuid.uuid4().bytes
        client_address = f"10.{octets[0]}.{octets[1]}.{octets[2]}"
        transport = httpx.ASGITransport(app=app, client=(client_address, 4000))
        client = httpx.AsyncClient(
            transport=transport, base_url=PUBLIC_URL, headers={"Origin": PUBLIC_URL}
        )
        contexts.append((lifespan, client))
        # Spy on the password check: True when a real stored hash was used.
        calls: list[bool] = []
        hasher = app.state.local_auth._hasher
        original = hasher.verify

        async def spy(stored: str | None, password: str) -> Any:
            calls.append(stored is not None)
            return await original(stored, password)

        hasher.verify = spy
        return Stack(client, app, settings, owner_engine, org_a, org_b, calls)

    yield build
    for lifespan, client in contexts:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


def unique(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def make_user(
    stack: Stack, *, role: str = "admin", status: str = "active", password: str = PASSWORD
) -> str:
    username = unique()
    await cli.create_admin(stack.settings, username=username, password=password)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE users SET role = :role, status = :status WHERE subject = :name"),
            {"role": role, "status": status, "name": username},
        )
    return username


async def login(stack: Stack, username: str, password: str = PASSWORD) -> httpx.Response:
    return await stack.client.post(
        "/api/auth/local/login", json={"username": username, "password": password}
    )


async def audit_rows(stack: Stack, org: uuid.UUID | None = None) -> list[dict[str, Any]]:
    async with stack.owner.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT actor_type, actor_id, action, target, outcome, ip_hash, details "
                "FROM audit_logs WHERE org_id = :org ORDER BY id"
            ),
            {"org": org or stack.org_a},
        )
        return [dict(row._mapping) for row in result]


def without_request_id(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    body.pop("request_id", None)
    return body  # type: ignore[no-any-return]


# --- configuration -----------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["off", "break-glass", "on"])
async def test_the_config_endpoint_is_public_and_reports_the_mode(
    make_stack: MakeStack, mode: str
) -> None:
    stack = await make_stack(mode)
    response = await stack.client.get("/api/auth/config")
    assert response.status_code == 200
    assert response.json() == {"local_login": mode, "oidc": None}


# --- signing in --------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_administrator_can_sign_in_and_use_the_session(make_stack: MakeStack) -> None:
    stack = await make_stack()
    username = await make_user(stack)
    response = await login(stack, username)
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["role"] == "admin"
    assert body["user"]["display_name"] == username
    assert body["csrf_token"]

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{HOST_PREFIXED_SESSION_COOKIE}=")
    for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"):
        assert attribute in cookie
    assert "Domain" not in cookie
    assert PASSWORD not in response.text

    me = await stack.client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["user"]["id"] == body["user"]["id"]
    assert me.json()["user"]["last_login_at"] is not None
    # The token from the sign-in is the one /me hands out too (same session, same seed).
    assert me.json()["csrf_token"] == body["csrf_token"]


@pytest.mark.anyio
async def test_the_username_is_normalized(make_stack: MakeStack) -> None:
    stack = await make_stack()
    username = await make_user(stack)
    assert (await login(stack, f"  {username.upper()} ")).status_code == 200


@pytest.mark.anyio
async def test_signing_in_writes_an_audit_row_without_the_password_or_the_address(
    make_stack: MakeStack,
) -> None:
    stack = await make_stack()
    username = await make_user(stack)
    await login(stack, username)
    rows = [r for r in await audit_rows(stack) if r["action"] == "auth.login"]
    assert len(rows) == 1
    row = rows[0]
    assert (row["outcome"], row["actor_type"]) == ("success", "local_admin")
    assert row["target"] == f"local:{username}"
    assert row["details"] == {"method": "local"}
    assert len(str(row["ip_hash"])) == 64  # a keyed hash, not the address
    assert PASSWORD not in str(row)


@pytest.mark.anyio
async def test_every_kind_of_failure_gives_the_same_answer(make_stack: MakeStack) -> None:
    stack = await make_stack()  # break-glass: administrators only
    admin = await make_user(stack)
    disabled = await make_user(stack, status="disabled")
    member = await make_user(stack, role="member")
    attempts = {
        "bad_password": (admin, "wrong password entirely"),
        "unknown_user": (unique("ghost"), PASSWORD),
        "inactive_user": (disabled, PASSWORD),
        "not_allowed": (member, PASSWORD),
        "password_too_long": (admin, "x" * 129),
    }
    answers = {}
    for reason, (name, password) in attempts.items():
        response = await login(stack, name, password)
        assert response.status_code == 401, reason
        assert "set-cookie" not in response.headers, reason
        answers[reason] = without_request_id(response)
    assert len({str(a) for a in answers.values()}) == 1
    assert answers["bad_password"]["code"] == "invalid_credentials"

    # The real reasons are for the operator, in the audit log.
    recorded = [
        (r["target"], r["details"]["reason"])
        for r in await audit_rows(stack)
        if r["outcome"] == "failure"
    ]
    assert (f"local:{admin}", "bad_password") in recorded
    assert (f"local:{admin}", "password_too_long") in recorded
    assert (f"local:{disabled}", "inactive_user") in recorded
    assert (f"local:{member}", "not_allowed") in recorded
    assert any(reason == "unknown_user" for _, reason in recorded)


@pytest.mark.anyio
async def test_unknown_accounts_cost_the_same_hashing_work(make_stack: MakeStack) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    await login(stack, admin, "wrong password entirely")
    await login(stack, unique("ghost"), "wrong password entirely")
    assert stack.verify_calls == [True, False]  # a stored hash, then the dummy: both verified


@pytest.mark.anyio
async def test_an_oversized_password_is_never_hashed(make_stack: MakeStack) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    response = await login(stack, admin, "x" * 1000)
    assert response.status_code == 401
    assert stack.verify_calls == []


@pytest.mark.anyio
async def test_members_may_sign_in_only_when_local_login_is_on(make_stack: MakeStack) -> None:
    on = await make_stack("on")
    member = await make_user(on, role="member")
    response = await login(on, member)
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "member"
    rows = [r for r in await audit_rows(on) if r["target"] == f"local:{member}"]
    assert rows[-1]["actor_type"] == "user"


@pytest.mark.anyio
async def test_local_login_off_looks_like_no_such_endpoint(make_stack: MakeStack) -> None:
    stack = await make_stack("off")
    admin = await make_user(stack)
    response = await login(stack, admin)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert "set-cookie" not in response.headers
    assert stack.verify_calls == []


# --- throttling --------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_repeated_failures_are_slowed_down_before_any_hashing(make_stack: MakeStack) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    statuses = [
        (await login(stack, admin, "wrong password entirely")).status_code for _ in range(5)
    ]
    assert statuses[:3] == [401, 401, 401]  # the free attempts
    assert 429 in statuses
    calls_before = len(stack.verify_calls)
    throttled = await login(stack, admin, PASSWORD)  # even the RIGHT password has to wait
    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) >= 1
    assert throttled.json()["code"] == "rate_limited"
    assert len(stack.verify_calls) == calls_before  # no hashing happened for the refused attempt


@pytest.mark.anyio
async def test_throttling_unknown_accounts_looks_the_same(make_stack: MakeStack) -> None:
    stack = await make_stack()
    ghost = unique("ghost")
    statuses = [
        (await login(stack, ghost, "wrong password entirely")).status_code for _ in range(5)
    ]
    assert statuses[:3] == [401, 401, 401]
    assert 429 in statuses


# --- sessions ----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_signing_in_replaces_any_earlier_session(make_stack: MakeStack) -> None:
    stack = await make_stack()
    first_user, second_user = await make_user(stack), await make_user(stack)
    first = await login(stack, first_user)
    old_cookie = stack.client.cookies[HOST_PREFIXED_SESSION_COOKIE]
    # A live session means the CSRF token is required, even for a sign-in.
    second = await stack.client.post(
        "/api/auth/local/login",
        json={"username": second_user, "password": PASSWORD},
        headers={"X-CSRF-Token": first.json()["csrf_token"]},
    )
    new_cookie = stack.client.cookies[HOST_PREFIXED_SESSION_COOKIE]
    assert first.status_code == second.status_code == 200
    assert new_cookie != old_cookie
    # The old id is dead, even for someone who kept a copy of it.
    other = httpx.AsyncClient(transport=httpx.ASGITransport(app=stack.app), base_url=PUBLIC_URL)
    try:
        stale = await other.get(
            "/api/me", headers={"Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={old_cookie}"}
        )
    finally:
        await other.aclose()
    assert stale.status_code == 401
    assert (await stack.client.get("/api/me")).json()["user"]["display_name"] == second_user


@pytest.mark.anyio
async def test_a_planted_session_cookie_is_not_adopted_at_sign_in(make_stack: MakeStack) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    planted = "P" * 43
    response = await stack.client.post(
        "/api/auth/local/login",
        json={"username": admin, "password": PASSWORD},
        headers={"Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={planted}"},
    )
    assert response.status_code == 200
    assert stack.client.cookies[HOST_PREFIXED_SESSION_COOKIE] != planted


@pytest.mark.anyio
async def test_logout_ends_the_session_and_clears_the_cookie(make_stack: MakeStack) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    token = (await login(stack, admin)).json()["csrf_token"]
    copied_cookie = stack.client.cookies[HOST_PREFIXED_SESSION_COOKIE]
    response = await stack.client.post("/api/auth/logout", headers={"X-CSRF-Token": token})
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert (await stack.client.get("/api/me")).status_code == 401
    # Forgetting the cookie in the browser is not enough: a copy of it must be dead on the server.
    thief = httpx.AsyncClient(transport=httpx.ASGITransport(app=stack.app), base_url=PUBLIC_URL)
    try:
        replay = await thief.get(
            "/api/me", headers={"Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={copied_cookie}"}
        )
    finally:
        await thief.aclose()
    assert replay.status_code == 401
    assert any(r["action"] == "auth.logout" for r in await audit_rows(stack))


@pytest.mark.anyio
async def test_logout_without_a_session_is_harmless(make_stack: MakeStack) -> None:
    stack = await make_stack()
    assert (await stack.client.post("/api/auth/logout")).status_code == 204


@pytest.mark.anyio
async def test_logout_needs_the_csrf_token_when_signed_in(make_stack: MakeStack) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    await login(stack, admin)
    refused = await stack.client.post("/api/auth/logout")
    assert refused.status_code == 403
    assert refused.json()["code"] == "csrf_failed"
    assert (await stack.client.get("/api/me")).status_code == 200  # still signed in


@pytest.mark.anyio
@pytest.mark.parametrize("origin", [None, "null", "https://evil.example"])
async def test_signing_in_from_another_site_is_refused(
    make_stack: MakeStack, origin: str | None
) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    headers = {"Origin": origin} if origin else {}
    request = stack.client.build_request(
        "POST", "/api/auth/local/login", json={"username": admin, "password": PASSWORD}
    )
    request.headers.pop("origin", None)
    request.headers.update(headers)
    response = await stack.client.send(request)
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert "set-cookie" not in response.headers


# --- password storage --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_weaker_hash_is_upgraded_at_sign_in(make_stack: MakeStack) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    from argon2 import PasswordHasher as Argon2
    from argon2 import Type

    weak = Argon2(time_cost=1, memory_cost=16, parallelism=1, type=Type.ID).hash(PASSWORD)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "UPDATE local_credentials SET password_hash = :hash "
                "WHERE user_id = (SELECT id FROM users WHERE subject = :name)"
            ),
            {"hash": weak, "name": admin},
        )
        before = (
            await connection.execute(
                text(
                    "SELECT password_changed_at FROM local_credentials "
                    "WHERE user_id = (SELECT id FROM users WHERE subject = :name)"
                ),
                {"name": admin},
            )
        ).scalar_one()
    assert (await login(stack, admin)).status_code == 200
    async with stack.owner.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT password_hash, password_changed_at FROM local_credentials "
                    "WHERE user_id = (SELECT id FROM users WHERE subject = :name)"
                ),
                {"name": admin},
            )
        ).one()
    assert row.password_hash != weak
    assert "m=64" in row.password_hash
    assert row.password_changed_at == before
    # The same password keeps working with the new hash.
    await stack.client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": (await stack.client.get("/api/me")).json()["csrf_token"]},
    )
    assert (await login(stack, admin)).status_code == 200


@pytest.mark.anyio
async def test_the_database_only_ever_holds_an_argon2id_hash(make_stack: MakeStack) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    async with stack.owner.connect() as connection:
        stored = (
            await connection.execute(
                text(
                    "SELECT password_hash FROM local_credentials "
                    "WHERE user_id = (SELECT id FROM users WHERE subject = :name)"
                ),
                {"name": admin},
            )
        ).scalar_one()
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


@pytest.mark.anyio
async def test_the_password_never_reaches_the_logs(
    make_stack: MakeStack, caplog: pytest.LogCaptureFixture
) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    with caplog.at_level(logging.DEBUG):
        await login(stack, admin, OTHER_PASSWORD)
        await login(stack, admin)
    assert OTHER_PASSWORD not in caplog.text
    assert PASSWORD not in caplog.text


# --- concurrency and failure -------------------------------------------------------------------


@pytest.mark.anyio
async def test_parallel_sign_ins_all_work_and_hashing_stays_bounded(make_stack: MakeStack) -> None:
    stack = await make_stack()
    users = [await make_user(stack) for _ in range(6)]
    running = peak = 0
    lock = threading.Lock()
    hasher = stack.app.state.local_auth._hasher
    original = hasher._check

    def slow(target: str, password: str) -> bool:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return original(target, password)  # type: ignore[no-any-return]

    hasher._check = slow
    clients = [
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stack.app, client=(f"198.51.100.{30 + i}", 1)),
            base_url=PUBLIC_URL,
            headers={"Origin": PUBLIC_URL},
        )
        for i in range(len(users))
    ]
    try:
        responses = await asyncio.gather(
            *(
                c.post("/api/auth/local/login", json={"username": u, "password": PASSWORD})
                for c, u in zip(clients, users, strict=True)
            )
        )
    finally:
        for c in clients:
            await c.aclose()
    assert [r.status_code for r in responses] == [200] * 6
    assert len({c.cookies[HOST_PREFIXED_SESSION_COOKIE] for c in clients}) == 6
    assert peak <= 2


@pytest.mark.anyio
async def test_when_redis_is_down_nobody_can_sign_in(make_stack: MakeStack) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    limiter = stack.app.state.login_throttle._limiter
    working = limiter._redis
    limiter._redis = Redis.from_url(
        "redis://127.0.0.1:1/1", socket_connect_timeout=0.2, socket_timeout=0.2
    )
    # The scripts were registered against the working client; register them on the dead one.
    limiter._hit = limiter._redis.register_script(limiter._hit.script)
    limiter._reserve_script = limiter._redis.register_script(limiter._reserve_script.script)
    limiter._refund_script = limiter._redis.register_script(limiter._refund_script.script)
    try:
        response = await login(stack, admin)
    finally:
        await limiter._redis.aclose()
        limiter._redis = working
    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"
    assert "set-cookie" not in response.headers


# --- /api/me -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_me_needs_a_session(make_stack: MakeStack) -> None:
    stack = await make_stack()
    assert (await stack.client.get("/api/me")).status_code == 401


@pytest.mark.anyio
async def test_me_reflects_a_demotion_and_a_disabled_account_at_once(
    make_stack: MakeStack,
) -> None:
    stack = await make_stack("on")
    admin = await make_user(stack)
    await login(stack, admin)
    assert (await stack.client.get("/api/me")).json()["user"]["role"] == "admin"
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE users SET role = 'member' WHERE subject = :name"), {"name": admin}
        )
    assert (await stack.client.get("/api/me")).json()["user"]["role"] == "member"
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE users SET status = 'disabled' WHERE subject = :name"), {"name": admin}
        )
    assert (await stack.client.get("/api/me")).status_code == 401


@pytest.mark.anyio
async def test_me_never_shows_another_organizations_user(make_stack: MakeStack) -> None:
    stack = await make_stack()
    store = stack.app.state.session_store
    async with stack.owner.begin() as connection:
        stranger = (
            await connection.execute(
                text(
                    "INSERT INTO users (org_id, issuer, subject, role) "
                    "VALUES (:org, 'local', :sub, 'admin') RETURNING id"
                ),
                {"org": stack.org_b, "sub": unique("stranger")},
            )
        ).scalar_one()
    # A session that names the other organization, and one that lies about it.
    for claimed_org in (stack.org_b, stack.org_a):
        session_id, _ = await store.create(stranger, claimed_org)
        response = await stack.client.get(
            "/api/me", headers={"Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={session_id}"}
        )
        assert response.status_code == 401
        assert "stranger" not in response.text


# --- review findings ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_parallel_burst_of_guesses_is_cut_off_before_the_hashing(
    make_stack: MakeStack,
) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    responses = await asyncio.gather(
        *(login(stack, admin, f"wrong password number {i}") for i in range(30))
    )
    statuses = [r.status_code for r in responses]
    # The free attempts and the one that sets the wait were hashed; the rest never were.
    assert statuses.count(401) <= 4
    assert statuses.count(429) >= 26
    assert len(stack.verify_calls) <= 4


@pytest.mark.anyio
async def test_a_user_that_exists_only_in_another_organization_cannot_sign_in(
    make_stack: MakeStack,
) -> None:
    stack = await make_stack()
    hasher = stack.app.state.local_auth._hasher
    stored = await hasher.hash(PASSWORD)
    name = unique("elsewhere")
    async with stack.owner.begin() as connection:
        user = (
            await connection.execute(
                text(
                    "INSERT INTO users (org_id, issuer, subject, role) "
                    "VALUES (:org, 'local', :sub, 'admin') RETURNING id"
                ),
                {"org": stack.org_b, "sub": name},
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO local_credentials (user_id, org_id, password_hash) "
                "VALUES (:u, :org, :h)"
            ),
            {"u": user, "org": stack.org_b, "h": stored},
        )
    assert (await login(stack, name)).status_code == 401  # right password, wrong organization


@pytest.mark.anyio
async def test_the_queries_filter_by_organization_even_without_row_level_security(
    make_stack: MakeStack,
) -> None:
    from pharos.identity import repository

    stack = await make_stack()
    name = unique("scoped")
    await cli.create_admin(stack.settings, username=name, password=PASSWORD)
    # The owner role bypasses RLS, so only the explicit org_id filter can keep the rows apart.
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(stack.owner) as session:
        assert await repository.find_local_user(session, stack.org_a, name) is not None
        assert await repository.find_local_user(session, stack.org_b, name) is None
        user_id = await repository.find_user_id_by_username(session, stack.org_a, name)
        assert user_id is not None
        assert await repository.find_user_id_by_username(session, stack.org_b, name) is None
        assert await repository.get_user_view(session, stack.org_b, user_id) is None
        assert not await repository.set_password_hash(session, stack.org_b, user_id, "$argon2id$x")


@pytest.mark.anyio
async def test_changing_the_password_ends_every_session_of_the_account(
    make_stack: MakeStack,
) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    other = await make_user(stack)
    await login(stack, admin)
    attacker_cookie = stack.client.cookies[HOST_PREFIXED_SESSION_COOKIE]
    await cli.set_password(stack.settings, username=admin, password="a brand new long passphrase")
    thief = httpx.AsyncClient(transport=httpx.ASGITransport(app=stack.app), base_url=PUBLIC_URL)
    try:
        replay = await thief.get(
            "/api/me", headers={"Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={attacker_cookie}"}
        )
    finally:
        await thief.aclose()
    assert replay.status_code == 401
    assert (await login(stack, admin, PASSWORD)).status_code == 401  # the old password is gone
    assert (await login(stack, other)).status_code == 200  # other accounts are untouched


@pytest.mark.anyio
async def test_a_typed_password_in_the_username_box_is_not_written_to_the_audit_log(
    make_stack: MakeStack,
) -> None:
    stack = await make_stack()
    secret_typed_as_name = "my secret passphrase is here"
    response = await login(stack, secret_typed_as_name)
    assert response.status_code == 401
    rows = await audit_rows(stack)
    assert not any(secret_typed_as_name in str(row) for row in rows)
    assert any(row["target"] == "local:(invalid)" for row in rows)


@pytest.mark.anyio
async def test_no_database_connection_is_held_while_a_sign_in_waits_for_hashing(
    make_stack: MakeStack,
) -> None:
    stack = await make_stack()
    admin = await make_user(stack)
    pool = stack.app.state.session_factory.kw["bind"].pool
    hasher = stack.app.state.local_auth._hasher
    held: list[int] = []
    original = hasher.verify

    async def watch(stored: str | None, password: str) -> object:
        held.append(pool.checkedout())
        return await original(stored, password)

    hasher.verify = watch
    for name in (admin, unique("ghost")):  # a known and an unknown account
        await login(stack, name, "a wrong password, but long enough")
    assert held == [0, 0]


@pytest.mark.anyio
async def test_a_demoted_administrator_loses_the_password_session_in_break_glass_mode(
    make_stack: MakeStack,
) -> None:
    stack = await make_stack()  # break-glass: administrators only
    admin = await make_user(stack)
    await login(stack, admin)
    assert (await stack.client.get("/api/me")).status_code == 200
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE users SET role = 'member' WHERE subject = :name"), {"name": admin}
        )
    assert (await stack.client.get("/api/me")).status_code == 401


@pytest.mark.anyio
async def test_a_password_session_of_a_member_ends_when_the_mode_is_tightened(
    make_stack: MakeStack,
) -> None:
    stack = await make_stack("on")
    member = await make_user(stack, role="member")
    await login(stack, member)
    assert (await stack.client.get("/api/me")).status_code == 200
    stack.app.state.settings.local_login = "break-glass"  # as after changing PHAROS_LOCAL_LOGIN
    assert (await stack.client.get("/api/me")).status_code == 401
