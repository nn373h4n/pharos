"""The request body limit (M5-04, F-25): declared and streamed sizes, the exact boundary, the format
of the refusal, and that a refused request never reaches the application."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request

from pharos.core.bodylimit import MAX_BODY_BYTES, BodyLimitMiddleware
from pharos.core.errors import PROBLEM_JSON, install_error_handlers

LIMIT = 1000


def build() -> tuple[FastAPI, list[int]]:
    reached: list[int] = []
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(BodyLimitMiddleware, max_bytes=LIMIT)

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        reached.append(len(body))
        return {"size": len(body)}

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    return app, reached


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.anyio
@pytest.mark.parametrize("size", [0, 1, LIMIT - 1, LIMIT])
async def test_a_body_up_to_the_limit_is_accepted(size: int) -> None:
    app, reached = build()
    async with client(app) as http:
        response = await http.post("/echo", content=b"x" * size)
    assert response.status_code == 200 and response.json() == {"size": size}
    assert reached == [size]


@pytest.mark.anyio
async def test_one_byte_over_the_limit_is_refused_before_the_application_runs() -> None:
    app, reached = build()
    async with client(app) as http:
        response = await http.post("/echo", content=b"x" * (LIMIT + 1))
    assert response.status_code == 413
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["code"] == "payload_too_large"
    assert response.headers["connection"] == "close"
    assert reached == []  # the handler never ran


@pytest.mark.anyio
async def test_a_streamed_body_without_a_length_is_cut_when_it_grows_past_the_limit() -> None:
    app, reached = build()

    async def pieces(total: int) -> AsyncIterator[bytes]:
        for _ in range(total // 100):
            yield b"x" * 100

    async with client(app) as http:
        ok = await http.post("/echo", content=pieces(LIMIT))  # the control: exactly the limit
        too_big = await http.post("/echo", content=pieces(LIMIT + 100))
    assert ok.status_code == 200 and ok.json() == {"size": LIMIT}
    assert too_big.status_code == 413
    assert reached == [LIMIT]


@pytest.mark.anyio
async def test_a_lying_content_length_cannot_get_more_through() -> None:
    app, reached = build()
    async with client(app) as http:
        response = await http.post(
            "/echo", content=b"x" * (LIMIT + 50), headers={"Content-Length": "10"}
        )
    # Whatever the server stack does with the mismatch, the handler never sees more than the limit.
    assert all(size <= LIMIT for size in reached)
    assert response.status_code in {200, 400, 413}


@pytest.mark.anyio
async def test_other_requests_are_not_touched() -> None:
    app, _ = build()
    async with client(app) as http:
        assert (await http.get("/ping")).json() == {"ok": "yes"}


def test_the_default_limit_is_one_mebibyte() -> None:
    assert MAX_BODY_BYTES == 1024 * 1024


@pytest.mark.anyio
async def test_a_declared_size_over_the_limit_is_refused_without_reading_any_of_the_body() -> None:
    app, reached = build()
    reads = 0
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * 10, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/echo",
        "raw_path": b"/echo",
        "query_string": b"",
        "headers": [(b"content-length", str(LIMIT + 1).encode())],
        "state": {},
    }
    await app(scope, receive, send)  # type: ignore[arg-type]
    assert sent[0]["status"] == 413
    assert reads == 0 and reached == []


@pytest.mark.anyio
async def test_a_framework_that_catches_the_cut_off_still_answers_413() -> None:
    """FastAPI reads a model body inside a try block and turns any failure into a 400: the
    middleware must still be the one that answers."""
    from pydantic import BaseModel

    class Payload(BaseModel):
        text: str

    reached: list[str] = []
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(BodyLimitMiddleware, max_bytes=LIMIT)

    @app.post("/model")
    async def model(payload: Payload) -> dict[str, int]:
        reached.append(payload.text)
        return {"size": len(payload.text)}

    async def pieces(total: int) -> AsyncIterator[bytes]:
        yield b'{"text": "'
        for _ in range(total // 100):
            yield b"x" * 100
        yield b'"}'

    async with client(app) as http:
        ok = await http.post(
            "/model", content=pieces(500), headers={"content-type": "application/json"}
        )
        big = await http.post(
            "/model", content=pieces(LIMIT + 100), headers={"content-type": "application/json"}
        )
    assert ok.status_code == 200  # the control: a streamed body inside the limit is read
    assert big.status_code == 413 and big.json()["code"] == "payload_too_large"
    assert len(reached) == 1
