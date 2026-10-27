from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from pharos.core.config import Settings
from pharos.core.errors import ApiError, ErrorCode, forbidden, not_authenticated, not_found
from pharos.main import create_app

ORIGIN = {"Origin": "http://localhost:3000"}  # the development default of PHAROS_PUBLIC_URL


class Login(BaseModel):
    password: str


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app: FastAPI = create_app(Settings(environment="test"))

    @app.get("/api/_t/forbidden")
    async def _forbidden() -> None:
        raise forbidden()

    @app.get("/api/_t/anonymous")
    async def _anonymous() -> None:
        raise not_authenticated()

    @app.get("/api/_t/missing")
    async def _missing() -> None:
        raise not_found()

    @app.get("/api/_t/limited")
    async def _limited() -> None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, 503, headers={"Retry-After": "5"})

    @app.post("/api/_t/login")
    async def _login(body: Login) -> None:
        return None

    @app.get("/api/_t/boom")
    async def _boom() -> None:
        raise RuntimeError("secret internal detail: db password hunter2")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/api/_t/forbidden", 403, "forbidden"),
        ("/api/_t/anonymous", 401, "not_authenticated"),
        ("/api/_t/missing", 404, "not_found"),
        ("/api/_t/nonexistent", 404, "not_found"),
    ],
)
async def test_errors_are_problem_documents(
    client: httpx.AsyncClient, path: str, status: int, code: str
) -> None:
    response = await client.get(path, headers={"X-Request-ID": "req-1"})
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == code
    assert body["status"] == status
    assert body["type"] == f"urn:pharos:problem:{code}"
    assert body["request_id"] == "req-1"
    assert response.headers["x-request-id"] == "req-1"


@pytest.mark.anyio
async def test_method_not_allowed_keeps_the_allow_header(client: httpx.AsyncClient) -> None:
    response = await client.delete("/api/_t/missing", headers=ORIGIN)
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"
    assert "GET" in response.headers["allow"]


@pytest.mark.anyio
async def test_api_error_can_carry_headers(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/_t/limited")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


@pytest.mark.anyio
async def test_validation_errors_name_the_field_but_never_echo_the_input(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/_t/login", json={"password": 12345678, "x": "pw-leak-canary"}, headers=ORIGIN
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["errors"] == [{"loc": ["body", "password"], "type": "string_type"}]
    assert "pw-leak-canary" not in response.text
    assert "12345678" not in response.text


@pytest.mark.anyio
async def test_unexpected_errors_hide_the_cause_and_keep_the_request_id(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = await client.get("/api/_t/boom", headers={"X-Request-ID": "req-500"})
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["request_id"] == "req-500"
    assert response.headers["x-request-id"] == "req-500"
    assert "hunter2" not in response.text
    assert any(r.getMessage() == "unhandled exception" for r in caplog.records)
