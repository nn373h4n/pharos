"""The body limit is part of the real application, in front of routing and the CSRF check."""

# ruff: noqa: F811  (fixtures are imported by name)

from collections.abc import AsyncIterator

import pytest

from pharos.core.bodylimit import MAX_BODY_BYTES

from .test_local_auth import MakeStack, Stack, make_stack  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_an_oversized_body_is_refused_by_the_real_app_and_a_normal_one_is_not(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    ok = await stack.client.post("/api/auth/local/login", json={"username": "a", "password": "b"})
    assert ok.status_code != 413  # the control: a normal body reaches the handler
    big = await stack.client.post(
        "/api/auth/local/login", content=b'{"username":"' + b"a" * (MAX_BODY_BYTES + 1) + b'"}'
    )
    assert big.status_code == 413
    assert big.json()["code"] == "payload_too_large"
    assert big.headers.get("x-request-id")  # inside the request id middleware


async def test_a_streamed_oversized_body_is_also_a_413_in_the_real_app(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")

    async def pieces() -> AsyncIterator[bytes]:
        yield b'{"username": "'
        for _ in range(MAX_BODY_BYTES // 1000 + 2):
            yield b"a" * 1000
        yield b'", "password": "b"}'

    response = await stack.client.post(
        "/api/auth/local/login", content=pieces(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
