import logging
import re
from collections.abc import AsyncIterator

import httpx
import pytest
from redis.asyncio import Redis

from pharos.core.config import Settings
from pharos.core.db import create_engine
from pharos.core.health import Readiness, ReadinessResponse
from pharos.core.organization import OrganizationResolver
from pharos.main import create_app

from ..constants import FAKE_SECRET_KEY

PASSWORD = "Zx9-example-only"
UNREACHABLE_DB = f"postgresql+asyncpg://user:{PASSWORD}@127.0.0.1:1/pharos"
UNREACHABLE_REDIS = f"redis://:{PASSWORD}@127.0.0.1:1/0"
ALL_OK = {
    "database": "ok",
    "database_role": "ok",
    "redis": "ok",
    "worker": "ok",
    "migrations": "ok",
    "organization": "ok",
}


class FakeReadiness:
    def __init__(self, checks: dict[str, str]) -> None:
        status = "ok" if all(v == "ok" for v in checks.values()) else "unavailable"
        self._report = ReadinessResponse.model_validate({"status": status, "checks": checks})

    async def check(self) -> ReadinessResponse:
        return self._report


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(environment="test"))
    app.state.readiness = FakeReadiness(ALL_OK)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        http.app = app  # type: ignore[attr-defined]
        yield http


@pytest.mark.anyio
async def test_healthz_is_ok_without_any_dependency(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_readyz_is_ok_when_every_check_passes(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": ALL_OK}


@pytest.mark.anyio
@pytest.mark.parametrize("failing", list(ALL_OK))
async def test_readyz_is_503_and_names_the_failed_check(
    client: httpx.AsyncClient, failing: str
) -> None:
    client.app.state.readiness = FakeReadiness(  # type: ignore[attr-defined]
        {**ALL_OK, failing: "fail"}
    )
    response = await client.get("/api/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["checks"][failing] == "fail"


@pytest.mark.anyio
async def test_request_id_is_generated_and_returned(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/healthz")
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


@pytest.mark.anyio
async def test_incoming_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/healthz", headers={"X-Request-ID": "trace-abc_1.2"})
    assert response.headers["x-request-id"] == "trace-abc_1.2"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "value", ["a" * 65, "a" * 500, "   ", "has space", 'quo"te', "semi;colon", "<script>", "a/b"]
)
async def test_unusable_request_ids_are_replaced(client: httpx.AsyncClient, value: str) -> None:
    response = await client.get("/api/healthz", headers={"X-Request-ID": value})
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


@pytest.mark.anyio
async def test_request_id_of_maximum_length_is_kept(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/healthz", headers={"X-Request-ID": "a" * 64})
    assert response.headers["x-request-id"] == "a" * 64


@pytest.mark.anyio
async def test_docs_are_available_outside_production(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/docs")).status_code == 200


@pytest.mark.anyio
async def test_docs_are_hidden_in_production() -> None:
    app = create_app(
        Settings(
            environment="production",
            database_url=f"postgresql+asyncpg://user:{PASSWORD}@db:5432/pharos",  # type: ignore[arg-type]
            redis_url=f"redis://:{PASSWORD}@cache:6379/0",  # type: ignore[arg-type]
            public_url="https://pharos.example",
            secret_key=FAKE_SECRET_KEY,  # type: ignore[arg-type]
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        assert (await http.get("/api/docs")).status_code == 404
        assert (await http.get("/api/openapi.json")).status_code == 404


@pytest.mark.anyio
async def test_real_readiness_fails_closed_and_does_not_leak_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_engine(UNREACHABLE_DB)
    redis = Redis.from_url(UNREACHABLE_REDIS)
    try:
        with caplog.at_level(logging.ERROR):
            report = await Readiness(
                engine, redis, frozenset({"0001"}), OrganizationResolver(engine, "acme")
            ).check()
    finally:
        await redis.aclose()
        await engine.dispose()

    assert report.status == "unavailable"
    assert report.checks == {key: "fail" for key in ALL_OK}
    body = report.model_dump_json()
    assert PASSWORD not in body
    assert "127.0.0.1" not in body
    # The reason is logged for operators.
    assert {r.getMessage() for r in caplog.records} >= {
        "readiness: database check failed",
        "readiness: database role check failed",
        "readiness: redis check failed",
        "readiness: worker heartbeat check failed",
        "readiness: migration check failed",
        "readiness: organization check failed",
    }
