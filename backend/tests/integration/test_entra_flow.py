"""Sign-in through the Microsoft Entra ID preset against an Entra-shaped fake provider."""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text

from ..oidc_fake import (
    CLIENT_ID,
    CLIENT_SECRET,
    ENTRA_ISSUER,
    ENTRA_OBJECT_ID,
    ENTRA_TENANT,
    FakeIdp,
    entra_claims,
    entra_idp,
)
from .test_local_auth import MakeStack, Stack, audit_rows, make_stack  # noqa: F401
from .test_oidc_flow import begin, error_code, failure_reasons, sign_in

pytestmark = pytest.mark.integration


async def entra_stack(make_stack: MakeStack, **extra: Any) -> tuple[Stack, FakeIdp]:
    fake = entra_idp()
    stack = await make_stack(
        "break-glass",
        oidc_preset="entra",
        entra_tenant_id=ENTRA_TENANT,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret=CLIENT_SECRET,
        **extra,
    )
    stack.app.state.oidc.idp._http = fake.client()
    return stack, fake


async def users(stack: Stack) -> list[dict[str, Any]]:
    async with stack.owner.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT id, issuer, subject, role, status, department_id, department_source "
                "FROM users WHERE org_id = :o AND issuer <> 'local'"
            ),
            {"o": stack.org_a},
        )
        return [dict(r._mapping) for r in rows]


@pytest.mark.anyio
async def test_the_preset_configures_the_issuer_the_name_and_the_scopes(
    make_stack: MakeStack,
) -> None:
    stack, _fake = await entra_stack(make_stack)
    assert stack.app.state.oidc.idp.issuer == ENTRA_ISSUER
    config = (await stack.client.get("/api/auth/config")).json()
    assert config["oidc"]["provider_name"] == "Microsoft"
    started = await begin(stack.client)
    query = parse_qs(urlsplit(started.headers["location"]).query)
    assert started.headers["location"].startswith(
        f"https://login.microsoftonline.com/{ENTRA_TENANT}/oauth2/v2.0/authorize?"
    )
    assert query["scope"] == ["openid profile email User.Read"]
    assert query["code_challenge_method"] == ["S256"]


@pytest.mark.anyio
async def test_the_person_is_the_object_id_and_the_department_comes_from_graph(
    make_stack: MakeStack,
) -> None:
    stack, fake = await entra_stack(make_stack)
    response = await sign_in(stack, fake, **entra_claims(fake))
    assert response.status_code == 303 and urlsplit(response.headers["location"]).path == "/"
    [user] = await users(stack)
    assert user["issuer"] == ENTRA_ISSUER
    assert user["subject"] == ENTRA_OBJECT_ID
    assert (user["role"], user["status"]) == ("member", "active")  # never from the provider
    assert user["department_source"] == "idp"
    me = (await stack.client.get("/api/me")).json()["user"]
    assert me["department"] == "Finance"
    assert [auth for _, auth in fake.graph_requests] == ["Bearer opaque-access-token"]


@pytest.mark.anyio
async def test_the_same_person_arriving_with_another_per_app_subject_is_one_user(
    make_stack: MakeStack,
) -> None:
    stack, fake = await entra_stack(make_stack)
    await sign_in(stack, fake, **entra_claims(fake, sub="per-app-one"))
    await sign_in(stack, fake, **entra_claims(fake, sub="per-app-two"))
    assert len(await users(stack)) == 1


@pytest.mark.anyio
async def test_a_graph_failure_never_stops_the_sign_in(make_stack: MakeStack) -> None:
    stack, fake = await entra_stack(make_stack)
    fake.graph_status = 503
    response = await sign_in(stack, fake, **entra_claims(fake))
    assert urlsplit(response.headers["location"]).path == "/"
    assert (await stack.client.get("/api/me")).status_code == 200
    [user] = await users(stack)
    assert user["department_id"] is None


@pytest.mark.anyio
async def test_the_access_token_is_not_kept(make_stack: MakeStack) -> None:
    stack, fake = await entra_stack(make_stack)
    await sign_in(stack, fake, **entra_claims(fake))
    assert "opaque-access-token" not in repr(await audit_rows(stack))
    async with stack.owner.connect() as connection:
        dump = repr((await connection.execute(text("SELECT * FROM users"))).all())
    assert "opaque-access-token" not in dump


@pytest.mark.anyio
async def test_a_token_that_is_not_from_our_directory_signs_nobody_in(
    make_stack: MakeStack,
) -> None:
    stack, fake = await entra_stack(make_stack)
    other = "7c6b5a49-3827-4615-9a03-b1c2d3e4f506"
    response = await sign_in(stack, fake, **entra_claims(fake, tid=other))
    assert error_code(response) == "sso_failed"
    assert (await stack.client.get("/api/me")).status_code == 401
    assert await users(stack) == []
    assert await failure_reasons(stack) == ["token:bad_tenant"]
    assert fake.graph_requests == []  # nothing was sent to Graph for it


@pytest.mark.anyio
async def test_an_old_format_token_signs_nobody_in(make_stack: MakeStack) -> None:
    stack, fake = await entra_stack(make_stack)
    response = await sign_in(stack, fake, **entra_claims(fake, ver="1.0"))
    assert error_code(response) == "sso_failed"
    assert await failure_reasons(stack) == ["token:bad_version"]


@pytest.mark.anyio
async def test_a_disabled_person_stays_disabled(make_stack: MakeStack) -> None:
    stack, fake = await entra_stack(make_stack)
    await sign_in(stack, fake, **entra_claims(fake))
    await stack.client.post("/api/auth/logout")
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE users SET status = 'disabled' WHERE issuer <> 'local'")
        )
    response = await sign_in(stack, fake, **entra_claims(fake))
    assert error_code(response) == "account_disabled"
    [user] = await users(stack)
    assert user["status"] == "disabled"
    assert (await stack.client.get("/api/me")).status_code == 401


@pytest.mark.anyio
@pytest.mark.parametrize("department", [" ", "\u00a0", "\u0000", "\u0000Finance\u001b[31m\n"])
async def test_an_odd_department_from_graph_never_breaks_the_sign_in(
    make_stack: MakeStack, department: str
) -> None:
    stack, fake = await entra_stack(make_stack)
    fake.graph_department = department
    response = await sign_in(stack, fake, **entra_claims(fake))
    assert urlsplit(response.headers["location"]).path == "/"
    assert (await stack.client.get("/api/me")).status_code == 200
    shown = (await stack.client.get("/api/me")).json()["user"]["department"]
    assert shown is None or not any(ord(char) < 0x20 for char in shown)
    if department.strip("\u00a0 \u0000"):
        assert shown is not None and shown.startswith("Finance")


@pytest.mark.anyio
async def test_a_guest_signs_nobody_in_unless_the_operator_allows_it(
    make_stack: MakeStack,
) -> None:
    stack, fake = await entra_stack(make_stack)
    response = await sign_in(stack, fake, **entra_claims(fake, acct=1))
    assert error_code(response) == "sso_failed"
    assert await failure_reasons(stack) == ["token:guest_not_allowed"]
    assert await users(stack) == []

    allowed, fake2 = await entra_stack(make_stack, entra_allow_guests=True)
    await sign_in(allowed, fake2, **entra_claims(fake2, acct=1))
    assert len(await users(allowed)) == 1
