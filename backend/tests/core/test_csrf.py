import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import Response

from pharos.core.config import Settings
from pharos.core.csrf import (
    HOST_PREFIXED_SESSION_COOKIE,
    SESSION_COOKIE,
    SessionCookie,
    csrf_token,
    origin_allowed,
    token_matches,
)
from pharos.core.errors import DependencyUnavailable
from pharos.core.secrets import KeyPurpose, derive_key
from pharos.core.sessions import SessionRecord
from pharos.main import create_app

from ..constants import FAKE_SECRET_KEY

PUBLIC_URL = "https://pharos.example"
SESSION_ID = "A" * 43
OTHER_SESSION_ID = "B" * 43
KEY = derive_key(
    Settings(environment="test", secret_key=FAKE_SECRET_KEY).secret_key, KeyPurpose.CSRF
)  # type: ignore[arg-type]


def record(seed: str = "seed-1") -> SessionRecord:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return SessionRecord(uuid.uuid4(), uuid.uuid4(), now, now, seed)


# --- pure functions ----------------------------------------------------------------------------


def test_the_token_is_bound_to_the_session_and_its_seed() -> None:
    session = record("seed-1")
    token = csrf_token(KEY, SESSION_ID, session)
    assert token_matches(KEY, SESSION_ID, session, token)
    assert not token_matches(KEY, OTHER_SESSION_ID, session, token)
    assert not token_matches(KEY, SESSION_ID, record("seed-2"), token)
    assert not token_matches(b"another key" * 3, SESSION_ID, session, token)


@pytest.mark.parametrize("supplied", [None, "", "x", "A" * 43])
def test_missing_or_wrong_tokens_do_not_match(supplied: str | None) -> None:
    assert not token_matches(KEY, SESSION_ID, record(), supplied)


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        (PUBLIC_URL, True),
        (None, False),
        ("null", False),
        ("", False),
        ("https://pharos.example/", False),
        ("http://pharos.example", False),
        ("https://pharos.example:8443", False),
        ("https://evil.example", False),
        ("https://pharos.example.evil.example", False),
        ("https://PHAROS.example", False),
    ],
)
def test_origin_must_equal_the_public_address_exactly(origin: str | None, allowed: bool) -> None:
    assert origin_allowed(origin, PUBLIC_URL) is allowed


def test_the_cookie_is_host_prefixed_and_secure_over_https() -> None:
    cookie = SessionCookie("https://pharos.example")
    response = Response()
    cookie.set(response, SESSION_ID)
    header = response.headers["set-cookie"]
    assert header.startswith(f"{HOST_PREFIXED_SESSION_COOKIE}={SESSION_ID};")
    for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"):
        assert attribute in header
    assert "Domain" not in header
    assert "Max-Age" not in header
    assert "Expires" not in header


def test_the_cookie_cannot_carry_the_prefix_without_https() -> None:
    cookie = SessionCookie("http://localhost:3000")
    response = Response()
    cookie.set(response, SESSION_ID)
    assert cookie.name == SESSION_COOKIE
    assert "Secure" not in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]


def test_clearing_the_cookie_keeps_the_attributes_the_prefix_requires() -> None:
    response = Response()
    SessionCookie("https://pharos.example").clear(response)
    header = response.headers["set-cookie"]
    assert header.startswith(f"{HOST_PREFIXED_SESSION_COOKIE}=;") or '=""' in header
    assert "Secure" in header
    assert "Path=/" in header
    assert "Max-Age=0" in header


# --- the middleware ----------------------------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionRecord] = {}
        self.down = False

    async def get(self, session_id: str) -> SessionRecord | None:
        if self.down:
            raise DependencyUnavailable
        return self.sessions.get(session_id)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
async def client(store: FakeStore) -> AsyncIterator[httpx.AsyncClient]:
    app: FastAPI = create_app(
        Settings(environment="test", public_url=PUBLIC_URL, secret_key=FAKE_SECRET_KEY)  # type: ignore[arg-type]
    )
    app.state.session_store = store

    @app.get("/api/_t/read")
    async def read() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/api/_t/write")
    async def write() -> dict[str, str]:
        return {"ok": "yes"}

    @app.delete("/api/_t/write")
    async def remove() -> dict[str, str]:
        return {"ok": "yes"}

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def cookie(cookies: dict[str, str]) -> dict[str, str]:
    # httpx deprecates per-request cookies; a Cookie header is the same thing on the wire.
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}


def signed_in(store: FakeStore, session_id: str = SESSION_ID) -> tuple[dict[str, str], str]:
    session = record()
    store.sessions[session_id] = session
    cookies = {HOST_PREFIXED_SESSION_COOKIE: session_id}
    return cookies, csrf_token(KEY, session_id, session)


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_safe_methods_need_no_origin_or_token(client: httpx.AsyncClient, method: str) -> None:
    response = await client.request(method, "/api/_t/read")
    # Routed normally: 200 for the route, 405 for the methods it does not offer. Never 403 or 5xx.
    assert response.status_code in (200, 405)


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_unsafe_methods_without_an_origin_are_refused(
    client: httpx.AsyncClient, method: str
) -> None:
    response = await client.request(method, "/api/_t/write")
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "origin", ["null", "https://evil.example", "http://pharos.example", PUBLIC_URL + "/"]
)
async def test_a_foreign_origin_is_refused(client: httpx.AsyncClient, origin: str) -> None:
    response = await client.post("/api/_t/write", headers={"Origin": origin})
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


@pytest.mark.anyio
async def test_the_host_header_does_not_make_an_origin_valid(client: httpx.AsyncClient) -> None:
    # The proxy rewrites Host, so an attacker-chosen Origin must not pass because it matches it.
    response = await client.post(
        "/api/_t/write", headers={"Origin": "https://evil.example", "Host": "evil.example"}
    )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_the_right_origin_is_enough_without_a_session(client: httpx.AsyncClient) -> None:
    # Sign-in has no session yet: the Origin check is what stops login CSRF.
    response = await client.post("/api/_t/write", headers={"Origin": PUBLIC_URL})
    assert response.status_code == 200


@pytest.mark.anyio
async def test_a_signed_in_request_needs_the_token(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    cookies, token = signed_in(store)
    origin = {"Origin": PUBLIC_URL}
    missing = await client.post("/api/_t/write", headers={**origin, **cookie(cookies)})
    wrong = await client.post(
        "/api/_t/write", headers={**origin, "X-CSRF-Token": "nope", **cookie(cookies)}
    )
    good = await client.post(
        "/api/_t/write", headers={**origin, "X-CSRF-Token": token, **cookie(cookies)}
    )
    assert (missing.status_code, wrong.status_code, good.status_code) == (403, 403, 200)
    assert missing.json()["code"] == wrong.json()["code"] == "csrf_failed"


@pytest.mark.anyio
async def test_a_token_of_another_session_is_refused(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    cookies, _ = signed_in(store, SESSION_ID)
    _, other_token = signed_in(store, OTHER_SESSION_ID)
    response = await client.post(
        "/api/_t/write",
        headers={"Origin": PUBLIC_URL, "X-CSRF-Token": other_token, **cookie(cookies)},
    )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_the_token_cannot_replace_the_origin(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    cookies, token = signed_in(store)
    response = await client.post(
        "/api/_t/write",
        headers={"Origin": "https://evil.example", "X-CSRF-Token": token, **cookie(cookies)},
    )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_a_cookie_for_a_dead_session_is_an_anonymous_request(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/_t/write",
        headers={"Origin": PUBLIC_URL, **cookie({HOST_PREFIXED_SESSION_COOKIE: "C" * 43})},
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_when_the_session_store_is_down_the_request_fails_closed(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    store.down = True
    response = await client.post(
        "/api/_t/write",
        headers={"Origin": PUBLIC_URL, **cookie({HOST_PREFIXED_SESSION_COOKIE: SESSION_ID})},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"


# --- WebSocket handshakes ----------------------------------------------------------------------


async def run_websocket(origin: str | None) -> tuple[list[dict[str, object]], bool]:
    from pharos.core.csrf import CsrfMiddleware

    reached = False

    async def inner(scope: object, receive: object, send: object) -> None:
        nonlocal reached
        reached = True

    middleware = CsrfMiddleware(
        inner,  # type: ignore[arg-type]
        public_url=PUBLIC_URL,
        key=KEY,
        cookie=SessionCookie(PUBLIC_URL),
    )
    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def receive() -> dict[str, object]:
        return {"type": "websocket.connect"}

    headers = [(b"origin", origin.encode())] if origin is not None else []
    await middleware({"type": "websocket", "headers": headers}, receive, send)  # type: ignore[arg-type]
    return sent, reached


@pytest.mark.anyio
@pytest.mark.parametrize("origin", [None, "null", "https://evil.example"])
async def test_a_websocket_handshake_from_another_origin_is_closed(origin: str | None) -> None:
    sent, reached = await run_websocket(origin)
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert not reached


@pytest.mark.anyio
async def test_a_websocket_handshake_from_the_public_origin_passes() -> None:
    sent, reached = await run_websocket(PUBLIC_URL)
    assert sent == []
    assert reached
