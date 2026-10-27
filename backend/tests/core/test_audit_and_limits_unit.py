import re
from collections.abc import AsyncIterator

import httpx
import pytest
from starlette.requests import Request

from pharos.core.audit import ActorType, AuditAction, Outcome, clean_details, ip_hash
from pharos.core.client import client_ip
from pharos.core.config import Settings
from pharos.core.errors import ErrorCode
from pharos.core.ratelimit import rate_limited, subject
from pharos.main import create_app

from ..constants import FAKE_SECRET_KEY

# --- audit details -----------------------------------------------------------------------------


def test_every_action_matches_the_database_constraint() -> None:
    # audit_logs_action_check: ^[a-z][a-z0-9_.]{2,63}$
    for action in AuditAction:
        assert re.fullmatch(r"[a-z][a-z0-9_.]{2,63}", action.value), action


def test_actor_types_and_outcomes_match_the_database_constraints() -> None:
    assert {a.value for a in ActorType} == {"user", "local_admin", "operator_cli", "system"}
    assert {o.value for o in Outcome} == {"success", "failure"}


def test_clean_details_keeps_scalars_and_shortens_text() -> None:
    cleaned = clean_details({"reason": "x" * 500, "attempts": 3, "ok": True, "note": None})
    assert cleaned == {"reason": "x" * 200, "attempts": 3, "ok": True, "note": None}
    assert clean_details(None) == {}


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "new_password",
        "access_token",
        "Cookie",
        "authorization",
        "client_secret",
        "api_key",
        "session_id",
        "csrf",
    ],
)
def test_details_that_look_like_secrets_are_refused(key: str) -> None:
    with pytest.raises(ValueError, match=r"secret|invalid"):
        clean_details({key.lower(): "value"})


@pytest.mark.parametrize("key", ["Upper", "1abc", "has space", "a" * 41, "", "dash-ed"])
def test_odd_detail_keys_are_refused(key: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        clean_details({key: 1})


@pytest.mark.parametrize("value", [{"a": 1}, [1, 2], object()])
def test_non_scalar_detail_values_are_refused(value: object) -> None:
    with pytest.raises(ValueError, match="scalar"):
        clean_details({"field": value})


def test_too_many_details_are_refused() -> None:
    with pytest.raises(ValueError, match="too many"):
        clean_details({f"k{i}": i for i in range(21)})


# --- address hashing ---------------------------------------------------------------------------


def test_the_ip_hash_is_keyed_stable_and_not_the_address() -> None:
    key_a, key_b = b"a" * 32, b"b" * 32
    first = ip_hash(key_a, "203.0.113.9")
    assert first == ip_hash(key_a, "203.0.113.9")
    assert first != ip_hash(key_b, "203.0.113.9")
    assert first != ip_hash(key_a, "203.0.113.10")
    assert first is not None
    assert "203.0.113.9" not in first
    assert len(first) == 64  # fits audit_logs_ip_hash_check
    assert ip_hash(key_a, None) is None
    assert ip_hash(key_a, "") is None


# --- rate-limit keys ---------------------------------------------------------------------------


def test_subjects_are_hashed_normalized_and_bounded() -> None:
    assert subject("Alice@Example.com") == subject("  alice@example.com ")
    assert subject("alice") != subject("bob")
    assert "alice" not in subject("alice")
    assert len(subject("x" * 100_000)) == 64
    # The separator keeps ("ab", "c") apart from ("a", "bc").
    assert subject("ab", "c") != subject("a", "bc")


# --- client address ----------------------------------------------------------------------------


def request_with(client: tuple[str, int] | None, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": client,
        }
    )


def test_the_client_address_is_the_peer_and_never_a_header() -> None:
    request = request_with(("198.51.100.4", 5000), {"X-Forwarded-For": "203.0.113.77"})
    assert client_ip(request) == "198.51.100.4"
    assert client_ip(request_with(None, {"X-Real-IP": "203.0.113.77"})) is None


# --- 429 ---------------------------------------------------------------------------------------


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(environment="test", secret_key=FAKE_SECRET_KEY))  # type: ignore[arg-type]

    @app.get("/api/_t/slow-down")
    async def slow_down() -> None:
        raise rate_limited(7)

    @app.get("/api/_t/slow-down-zero")
    async def slow_down_zero() -> None:
        raise rate_limited(0)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.mark.anyio
async def test_rate_limited_answers_429_with_retry_after(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/_t/slow-down")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.json()["code"] == ErrorCode.RATE_LIMITED.value
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.anyio
async def test_retry_after_is_at_least_one_second(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/_t/slow-down-zero")).headers["retry-after"] == "1"
