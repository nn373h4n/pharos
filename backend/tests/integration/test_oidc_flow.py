"""Single sign-on through the real application against a fake provider: the happy path and the
negative suite (ADR-0011)."""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import asyncio
import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from pharos.core.csrf import HOST_PREFIXED_SESSION_COOKIE

from ..oidc_fake import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIdp, compact
from .test_local_auth import (  # noqa: F401  (make_stack is a fixture used by name)
    PUBLIC_URL,
    MakeStack,
    Stack,
    audit_rows,
    make_stack,
    unique,
)

pytestmark = pytest.mark.integration

BINDING_COOKIE = "__Host-pharos_oidc"


async def sso_stack(make_stack: MakeStack, **extra: Any) -> tuple[Stack, FakeIdp]:
    fake = FakeIdp()
    mode = extra.pop("local_login", "break-glass")
    stack = await make_stack(
        mode,
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret=CLIENT_SECRET,
        **extra,
    )
    stack.app.state.oidc.idp._http = fake.client()  # the provider is the fake one
    return stack, fake


def second_browser(stack: Stack) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app, client=("203.0.113.99", 1)),
        base_url=PUBLIC_URL,
    )


async def begin(client: httpx.AsyncClient, next_path: str | None = None) -> httpx.Response:
    params = {"next": next_path} if next_path is not None else None
    return await client.get("/api/auth/oidc/login", params=params)


def error_code(response: httpx.Response) -> str | None:
    assert response.status_code == 303, response.text
    location = urlsplit(response.headers["location"])
    if location.path != "/login":
        return None
    return parse_qs(location.query).get("error", [None])[0]


async def sign_in(
    stack: Stack,
    fake: FakeIdp,
    client: httpx.AsyncClient | None = None,
    *,
    next_path: str | None = None,
    **claims: Any,
) -> httpx.Response:
    client = client or stack.client
    started = await begin(client, next_path)
    assert started.status_code == 303
    _, params = fake.authorize(started.headers["location"], **claims)
    return await client.get("/api/auth/oidc/callback", params=params)


async def user_rows(stack: Stack, subject: str) -> list[dict[str, Any]]:
    async with stack.owner.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT id, role, status, email, display_name, department_id, department_source "
                "FROM users WHERE org_id = :o AND issuer = :i AND subject = :s"
            ),
            {"o": stack.org_a, "i": ISSUER, "s": subject},
        )
        return [dict(r._mapping) for r in rows]


async def failure_reasons(stack: Stack) -> list[str]:
    return [
        r["details"]["reason"]
        for r in await audit_rows(stack)
        if r["outcome"] == "failure" and r["target"] == "oidc"
    ]


# --- configuration -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_without_a_provider_the_sso_routes_do_not_exist(make_stack: MakeStack) -> None:
    stack = await make_stack()
    assert (await stack.client.get("/api/auth/config")).json()["oidc"] is None
    for path in ("/api/auth/oidc/login", "/api/auth/oidc/callback"):
        response = await stack.client.get(path)
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"


@pytest.mark.anyio
async def test_the_config_names_the_provider_for_the_sign_in_page(make_stack: MakeStack) -> None:
    stack, _ = await sso_stack(make_stack, oidc_provider_name="Company login")
    config = (await stack.client.get("/api/auth/config")).json()
    assert config["oidc"] == {"provider_name": "Company login", "login_url": "/api/auth/oidc/login"}


# --- the happy path ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_login_redirect_carries_state_nonce_pkce_and_binds_the_browser(
    make_stack: MakeStack,
) -> None:
    stack, _ = await sso_stack(make_stack)
    response = await begin(stack.client)
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        f"{ISSUER}/protocol/openid-connect/auth"
    )
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == f"{PUBLIC_URL}/api/auth/oidc/callback"
    assert query["scope"] == "openid profile email"
    assert query["code_challenge_method"] == "S256"
    assert query["response_mode"] == "query"
    assert len(query["state"]) >= 43 and len(query["nonce"]) >= 43
    assert len(query["code_challenge"]) == 43
    assert "code_verifier" not in query and "client_secret" not in query
    cookie = response.headers["set-cookie"]
    for attribute in (
        BINDING_COOKIE,
        "Secure",
        "HttpOnly",
        "SameSite=lax",
        "Path=/",
        "Max-Age=600",
    ):
        assert attribute in cookie
    assert "Domain" not in cookie


@pytest.mark.anyio
async def test_each_login_gets_fresh_secrets(make_stack: MakeStack) -> None:
    stack, _ = await sso_stack(make_stack)
    first = parse_qs(urlsplit((await begin(stack.client)).headers["location"]).query)
    second = parse_qs(urlsplit((await begin(stack.client)).headers["location"]).query)
    for field in ("state", "nonce", "code_challenge"):
        assert first[field] != second[field]


@pytest.mark.anyio
async def test_a_successful_sign_in_creates_the_user_and_a_session(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    subject = unique("sub")
    response = await sign_in(stack, fake, sub=subject, email="Ann@Example.test", name="Ann Lee")
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert response.headers["cache-control"] == "no-store"
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith(f"{HOST_PREFIXED_SESSION_COOKIE}="))
    for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"):
        assert attribute in session_cookie
    binding = next(c for c in cookies if c.startswith(f"{BINDING_COOKIE}="))
    assert "Max-Age=0" in binding  # the one-time cookie is cleared

    [user] = await user_rows(stack, subject)
    user_id = user["id"]
    assert user["role"] == "member"  # roles come from administrators, never from the provider
    assert user["status"] == "active"
    assert user["email"] == "Ann@Example.test"
    assert user["display_name"] == "Ann Lee"

    me = (await stack.client.get("/api/me")).json()
    assert me["user"]["id"] == str(user["id"])
    assert me["user"]["role"] == "member"
    assert me["csrf_token"]
    record = await stack.app.state.session_store.get(
        stack.client.cookies[HOST_PREFIXED_SESSION_COOKIE]
    )
    assert record is not None
    assert record.method == "oidc"

    actions = [(r["action"], r["outcome"]) for r in await audit_rows(stack)]
    assert ("user.created", "success") in actions
    assert ("auth.login", "success") in actions
    login = next(r for r in await audit_rows(stack) if r["action"] == "auth.login")
    assert login["details"] == {"method": "oidc"}
    assert login["target"] == f"user:{user_id}"


@pytest.mark.anyio
async def test_the_tokens_and_secrets_are_not_stored_or_logged(
    make_stack: MakeStack, caplog: pytest.LogCaptureFixture
) -> None:
    stack, fake = await sso_stack(make_stack)
    with caplog.at_level(logging.DEBUG):
        response = await sign_in(stack, fake, sub=unique("sub"))
    assert response.status_code == 303
    assert CLIENT_SECRET not in caplog.text
    assert "opaque-access-token" not in caplog.text
    assert "id_token" not in caplog.text.replace("id_token_hint", "")
    async with stack.owner.connect() as connection:
        columns = (
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name IN ('users', 'local_credentials')"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert not any("token" in column for column in columns)


@pytest.mark.anyio
async def test_a_second_sign_in_is_the_same_user_and_keeps_role_and_status(
    make_stack: MakeStack,
) -> None:
    stack, fake = await sso_stack(make_stack)
    subject = unique("sub")
    await sign_in(stack, fake, sub=subject)
    [first] = await user_rows(stack, subject)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE users SET role = 'admin' WHERE id = :id"), {"id": first["id"]}
        )
    await sign_in(stack, fake, sub=subject, email=None, name="Renamed")
    [second] = await user_rows(stack, subject)
    assert second["id"] == first["id"]
    assert second["role"] == "admin"  # the provider cannot change it
    assert second["email"] == first["email"]  # a missing claim does not erase what we know
    assert second["display_name"] == "Renamed"


@pytest.mark.anyio
async def test_a_disabled_user_cannot_sign_in_and_is_not_reactivated(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    subject = unique("sub")
    await sign_in(stack, fake, sub=subject)
    [user] = await user_rows(stack, subject)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text("UPDATE users SET status = 'disabled' WHERE id = :id"), {"id": user["id"]}
        )
    fresh = second_browser(stack)
    try:
        response = await sign_in(stack, fake, fresh, sub=subject)
    finally:
        pass
    assert error_code(response) == "account_disabled"
    assert HOST_PREFIXED_SESSION_COOKIE not in response.headers.get("set-cookie", "")
    [after] = await user_rows(stack, subject)
    assert after["status"] == "disabled"
    assert "inactive_user" in await failure_reasons(stack)
    await fresh.aclose()


@pytest.mark.anyio
async def test_signing_in_replaces_an_earlier_session(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    await sign_in(stack, fake, sub=unique("one"))
    old_cookie = stack.client.cookies[HOST_PREFIXED_SESSION_COOKIE]
    await sign_in(stack, fake, sub=unique("two"))
    assert stack.client.cookies[HOST_PREFIXED_SESSION_COOKIE] != old_cookie
    assert await stack.app.state.session_store.get(old_cookie) is None


# --- who a person is: never by e-mail ----------------------------------------------------------


@pytest.mark.anyio
async def test_the_same_e_mail_never_merges_accounts(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    shared = f"{unique('shared')}@example.test"
    # A local account with that address, and two single sign-on subjects claiming it.
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (org_id, issuer, subject, email, role) "
                "VALUES (:o, 'local', :s, :e, 'admin')"
            ),
            {"o": stack.org_a, "s": unique("local"), "e": shared},
        )
    first, second = unique("sso"), unique("sso")
    for subject in (first, second):
        assert (await sign_in(stack, fake, sub=subject, email=shared)).status_code == 303
        me = (await stack.client.get("/api/me")).json()["user"]
        assert me["role"] == "member"  # not the administrator who shares the address
    [a] = await user_rows(stack, first)
    [b] = await user_rows(stack, second)
    assert a["id"] != b["id"]


@pytest.mark.anyio
async def test_parallel_first_sign_ins_create_one_user_and_one_department(
    make_stack: MakeStack,
) -> None:
    stack, fake = await sso_stack(make_stack)
    subject, department = unique("race"), unique("dept")

    async def one() -> httpx.Response:
        client = second_browser(stack)
        try:
            return await sign_in(stack, fake, client, sub=subject, department=department)
        finally:
            await client.aclose()

    responses = await asyncio.gather(*(one() for _ in range(8)))
    assert [r.status_code for r in responses] == [303] * 8
    assert all(error_code(r) is None for r in responses)
    assert len(await user_rows(stack, subject)) == 1
    async with stack.owner.connect() as connection:
        count = (
            await connection.execute(
                text("SELECT count(*) FROM departments WHERE org_id = :o AND name = :n"),
                {"o": stack.org_a, "n": department},
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.anyio
async def test_the_department_comes_from_the_provider_unless_the_person_chose_one(
    make_stack: MakeStack,
) -> None:
    stack, fake = await sso_stack(make_stack)
    subject = unique("sub")
    await sign_in(stack, fake, sub=subject, department="Finance")
    [user] = await user_rows(stack, subject)
    assert user["department_source"] == "idp"
    me = (await stack.client.get("/api/me")).json()["user"]
    assert me["department"] == "Finance"

    await sign_in(stack, fake, sub=subject, department="Treasury")  # the provider moved them
    assert (await stack.client.get("/api/me")).json()["user"]["department"] == "Treasury"

    async with stack.owner.begin() as connection:  # now the person picks their own
        await connection.execute(
            text("UPDATE users SET department_source = 'user' WHERE id = :i"), {"i": user["id"]}
        )
    await sign_in(stack, fake, sub=subject, department="Legal")
    assert (await stack.client.get("/api/me")).json()["user"]["department"] == "Treasury"


# --- where to go next --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_page_on_this_site_is_kept_as_the_target(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    response = await sign_in(stack, fake, next_path="/feed?topic=a&b=c#x", sub=unique("s"))
    assert response.headers["location"] == "/feed?topic=a&b=c#x"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example",
        "http://evil.example/x",
        "//evil.example",
        "///evil.example",
        "/\\evil.example",
        "\\\\evil.example",
        "javascript:alert(1)",
        "evil.example",
        "/api/me",
        "/api/auth/oidc/login",
        "/ok\r\nSet-Cookie: x=1",
        "/ok\tpath",
        "/" + "a" * 600,
        "",
    ],
)
async def test_the_target_can_only_be_a_page_on_this_site(
    make_stack: MakeStack, target: str
) -> None:
    stack, fake = await sso_stack(make_stack)
    response = await sign_in(stack, fake, next_path=target, sub=unique("s"))
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# --- the negative suite ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_changed_state_is_refused(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    params["state"] = params["state"][:-3] + "AAA"
    response = await stack.client.get("/api/auth/oidc/callback", params=params)
    assert error_code(response) == "sso_failed"
    assert stack.client.cookies.get(HOST_PREFIXED_SESSION_COOKIE) is None
    assert await failure_reasons(stack) == []  # anyone can send these: logged, not stored per row


@pytest.mark.anyio
@pytest.mark.parametrize("drop", ["state", "code"])
async def test_a_callback_missing_state_or_code_is_refused(
    make_stack: MakeStack, drop: str
) -> None:
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    del params[drop]
    response = await stack.client.get("/api/auth/oidc/callback", params=params)
    assert error_code(response) == "sso_failed"


@pytest.mark.anyio
async def test_a_callback_without_any_parameters_is_refused(make_stack: MakeStack) -> None:
    stack, _ = await sso_stack(make_stack)
    assert error_code(await stack.client.get("/api/auth/oidc/callback")) == "sso_failed"


@pytest.mark.anyio
async def test_a_callback_replayed_is_refused(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    code, params = fake.authorize(started.headers["location"], sub=unique("s"))
    first = await stack.client.get("/api/auth/oidc/callback", params=params)
    assert error_code(first) is None
    # Even if the provider were willing to honour the code again, our spent state refuses it.
    fake.codes[code]["used"] = False
    # The binding cookie is gone after the first use; put it back to prove the state is spent too.
    stack.client.cookies.set(BINDING_COOKIE, started.cookies.get(BINDING_COOKIE) or "")
    again = await stack.client.get("/api/auth/oidc/callback", params=params)
    assert error_code(again) == "sso_failed"


@pytest.mark.anyio
async def test_the_callback_from_another_browser_is_refused(make_stack: MakeStack) -> None:
    """Login CSRF: the victim opens the attacker's callback link in their own browser."""
    stack, fake = await sso_stack(make_stack)
    attacker, victim = stack.client, second_browser(stack)
    try:
        started = await begin(attacker)
        _, params = fake.authorize(started.headers["location"], sub=unique("attacker"))
        response = await victim.get("/api/auth/oidc/callback", params=params)
        assert error_code(response) == "sso_failed"
        assert victim.cookies.get(HOST_PREFIXED_SESSION_COOKIE) is None  # not signed in as them
        # With a binding cookie of its own, still refused: it does not match the stored state.
        victim.cookies.set(BINDING_COOKIE, "V" * 43)
        again = await victim.get("/api/auth/oidc/callback", params=params)
        assert error_code(again) == "sso_failed"
    finally:
        await victim.aclose()
    # The attacker's own browser can still finish its own flow.
    assert error_code(await attacker.get("/api/auth/oidc/callback", params=params)) is None


@pytest.mark.anyio
async def test_a_callback_without_the_binding_cookie_is_refused(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    stack.client.cookies.delete(BINDING_COOKIE)
    assert error_code(await stack.client.get("/api/auth/oidc/callback", params=params)) == (
        "sso_failed"
    )


@pytest.mark.anyio
async def test_the_first_value_of_a_repeated_parameter_wins(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    evil = "evil-state-value"
    good_first = await stack.client.get(
        f"/api/auth/oidc/callback?state={params['state']}&state={evil}&code={params['code']}"
    )
    assert error_code(good_first) is None
    stack2, fake2 = await sso_stack(make_stack)
    started = await begin(stack2.client)
    _, params = fake2.authorize(started.headers["location"], sub=unique("s"))
    evil_first = await stack2.client.get(
        f"/api/auth/oidc/callback?state={evil}&state={params['state']}&code={params['code']}"
    )
    assert error_code(evil_first) == "sso_failed"


@pytest.mark.anyio
async def test_an_expired_state_is_refused(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    redis: Redis = stack.app.state.oidc._redis
    keys = [k async for k in redis.scan_iter("pharos:oidc:*")]
    assert keys
    for key in keys:
        assert 0 < await redis.ttl(key) <= 600
    await redis.delete(*keys)  # what the expiry does
    assert error_code(await stack.client.get("/api/auth/oidc/callback", params=params)) == (
        "sso_failed"
    )


@pytest.mark.anyio
async def test_a_code_that_was_already_used_at_the_provider_is_refused(
    make_stack: MakeStack,
) -> None:
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    code, params = fake.authorize(started.headers["location"], sub=unique("s"))
    fake.codes[code]["used"] = True
    assert error_code(await stack.client.get("/api/auth/oidc/callback", params=params)) == (
        "sso_failed"
    )
    assert "code_rejected" in await failure_reasons(stack)


@pytest.mark.anyio
async def test_a_wrong_pkce_challenge_at_the_provider_breaks_the_exchange(
    make_stack: MakeStack,
) -> None:
    """If the verifier we hold does not match the challenge the provider holds, nothing works."""
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    code, params = fake.authorize(started.headers["location"], sub=unique("s"))
    fake.codes[code]["challenge"] = "x" * 43  # as if another party had started the request
    assert error_code(await stack.client.get("/api/auth/oidc/callback", params=params)) == (
        "sso_failed"
    )
    assert "code_rejected" in await failure_reasons(stack)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"nonce": "forged"}, "token:bad_nonce"),
        ({"exp": int(time.time()) - 3600}, "token:expired"),
        ({"aud": "another-client"}, "token:bad_claims"),
        ({"iss": "https://evil.test/realms/pharos"}, "token:bad_claims"),
        ({"iat": int(time.time()) + 3600, "exp": int(time.time()) + 7200}, "token:bad_claims"),
        ({"aud": [CLIENT_ID, "other"]}, "token:bad_authorized_party"),
        ({"sub": "s" * 300}, "token:bad_subject"),
    ],
)
async def test_a_token_with_wrong_claims_is_refused(
    make_stack: MakeStack, claims: dict[str, Any], reason: str
) -> None:
    stack, fake = await sso_stack(make_stack)
    subject = unique("s")
    claims = {"sub": subject, **claims}
    response = await sign_in(stack, fake, **claims)
    assert error_code(response) == "sso_failed"
    assert reason in await failure_reasons(stack)
    assert HOST_PREFIXED_SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert await user_rows(stack, subject) == []  # nobody was created


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["none", "hs256", "rs512", "wrong_key", "unknown_kid"])
async def test_a_token_signed_the_wrong_way_is_refused(make_stack: MakeStack, kind: str) -> None:
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    claims = fake.claims(sub=unique("s"), nonce="whatever")
    forged = {
        "none": lambda: fake.none_token(claims),
        "hs256": lambda: fake.hs256_token(claims),
        "rs512": lambda: compact({"alg": "RS512", "kid": "rsa-1"}, claims, b"sig"),
        "wrong_key": lambda: FakeIdp().sign(claims),
        "unknown_kid": lambda: _unknown_kid(fake, claims),
    }[kind]()
    fake.token_body = f'{{"id_token": "{forged}"}}'.encode()
    response = await stack.client.get("/api/auth/oidc/callback", params=params)
    assert error_code(response) == "sso_failed"
    assert HOST_PREFIXED_SESSION_COOKIE not in response.headers.get("set-cookie", "")


def _unknown_kid(fake: FakeIdp, claims: dict[str, Any]) -> str:
    from joserfc.jwk import RSAKey

    stranger = RSAKey.generate_key(2048, parameters={"kid": "never-published", "use": "sig"})
    return fake.sign(claims, key=stranger)


@pytest.mark.anyio
async def test_a_rotated_signing_key_is_followed(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    await sign_in(stack, fake, sub=unique("before"))  # caches the key set
    fake.rotate()
    stack.app.state.oidc.idp._keys_at -= 3600  # the cached copy is old enough to be refetched
    fresh = second_browser(stack)
    try:
        response = await sign_in(stack, fake, fresh, sub=unique("after"))
    finally:
        await fresh.aclose()
    assert error_code(response) is None


@pytest.mark.anyio
async def test_a_mismatched_issuer_parameter_is_refused(make_stack: MakeStack) -> None:
    """RFC 9207: a response that says it comes from another provider is a mix-up attack."""
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    params["iss"] = "https://evil.test/realms/pharos"
    response = await stack.client.get("/api/auth/oidc/callback", params=params)
    assert error_code(response) == "sso_failed"
    assert "issuer_mismatch" in await failure_reasons(stack)
    # The right issuer parameter is fine.
    stack2, fake2 = await sso_stack(make_stack)
    started = await begin(stack2.client)
    _, params = fake2.authorize(started.headers["location"], sub=unique("s"))
    params["iss"] = ISSUER
    assert error_code(await stack2.client.get("/api/auth/oidc/callback", params=params)) is None


@pytest.mark.anyio
async def test_the_provider_saying_no_is_not_an_error_page(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    denied = await stack.client.get(
        "/api/auth/oidc/callback", params={"error": "access_denied", "state": params["state"]}
    )
    assert error_code(denied) == "sso_denied"
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    broken = await stack.client.get(
        "/api/auth/oidc/callback", params={"error": "server_error", "state": params["state"]}
    )
    assert error_code(broken) == "sso_failed"
    reasons = await failure_reasons(stack)
    assert "provider_denied" in reasons
    assert "provider_error:server_error" in reasons


@pytest.mark.anyio
async def test_an_error_callback_still_spends_the_state(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    await stack.client.get(
        "/api/auth/oidc/callback", params={"error": "access_denied", "state": params["state"]}
    )
    stack.client.cookies.set(BINDING_COOKIE, started.cookies.get(BINDING_COOKIE) or "")
    assert error_code(await stack.client.get("/api/auth/oidc/callback", params=params)) == (
        "sso_failed"
    )


# --- the provider is down ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_provider_outage_at_the_start_sends_the_person_back_with_a_notice(
    make_stack: MakeStack,
) -> None:
    stack, fake = await sso_stack(make_stack)
    fake.discovery_status = 503
    assert error_code(await begin(stack.client)) == "sso_unavailable"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["status", "network"])
async def test_a_provider_outage_at_the_token_step_is_reported_as_unavailable(
    make_stack: MakeStack, failure: str
) -> None:
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    if failure == "status":
        fake.token_status = 503
    else:
        fake.raise_on = {"/token": httpx.ConnectError("down")}
    response = await stack.client.get("/api/auth/oidc/callback", params=params)
    assert error_code(response) == "sso_unavailable"


@pytest.mark.anyio
async def test_a_provider_that_claims_another_issuer_is_refused(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    fake.discovery = {"issuer": "https://evil.test/realms/pharos"}
    assert error_code(await begin(stack.client)) == "sso_unavailable"


@pytest.mark.anyio
async def test_when_redis_is_down_single_sign_in_is_unavailable_not_open(
    make_stack: MakeStack,
) -> None:
    stack, _ = await sso_stack(make_stack)
    service = stack.app.state.oidc
    working = service._redis
    service._redis = Redis.from_url(
        "redis://127.0.0.1:1/1", socket_connect_timeout=0.2, socket_timeout=0.2
    )
    try:
        response = await begin(stack.client)
    finally:
        await service._redis.aclose()
        service._redis = working
    assert error_code(response) == "sso_unavailable"


@pytest.mark.anyio
async def test_too_many_sign_in_attempts_from_one_address_are_slowed(make_stack: MakeStack) -> None:
    stack, _ = await sso_stack(make_stack)
    results = [error_code(await begin(stack.client)) for _ in range(124)]
    assert results[:120] == [None] * 120
    assert "rate_limited" in results[120:]


# --- signing out -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_signing_out_of_an_sso_session_points_to_the_providers_logout(
    make_stack: MakeStack,
) -> None:
    stack, fake = await sso_stack(make_stack)
    await sign_in(stack, fake, sub=unique("s"))
    token = (await stack.client.get("/api/me")).json()["csrf_token"]
    response = await stack.client.post(
        "/api/auth/logout", headers={"Origin": PUBLIC_URL, "X-CSRF-Token": token}
    )
    assert response.status_code == 200
    url = urlsplit(response.json()["end_session_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/protocol/openid-connect/logout"
    query = parse_qs(url.query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["post_logout_redirect_uri"] == [f"{PUBLIC_URL}/login"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert (await stack.client.get("/api/me")).status_code == 401


@pytest.mark.anyio
async def test_a_provider_without_an_end_session_endpoint_gives_no_url(
    make_stack: MakeStack,
) -> None:
    stack, fake = await sso_stack(make_stack)
    fake.discovery = {"end_session_endpoint": None}
    await sign_in(stack, fake, sub=unique("s"))
    token = (await stack.client.get("/api/me")).json()["csrf_token"]
    response = await stack.client.post(
        "/api/auth/logout", headers={"Origin": PUBLIC_URL, "X-CSRF-Token": token}
    )
    assert response.status_code == 200
    assert response.json() == {"end_session_url": None}


@pytest.mark.anyio
async def test_when_password_sign_in_is_off_password_sessions_end_but_sso_ones_stay(
    make_stack: MakeStack,
) -> None:
    stack, _ = await sso_stack(make_stack, local_login="off")
    # (the app is built below with PHAROS_LOCAL_LOGIN=off, as a deployment would)
    store = stack.app.state.session_store
    async with stack.owner.begin() as connection:
        admin = (
            await connection.execute(
                text(
                    "INSERT INTO users (org_id, issuer, subject, role) "
                    "VALUES (:o, 'local', :s, 'admin') RETURNING id"
                ),
                {"o": stack.org_a, "s": unique("local")},
            )
        ).scalar_one()
    local_id, _ = await store.create(admin, stack.org_a)
    oidc_id, _ = await store.create(admin, stack.org_a, method="oidc")
    other = second_browser(stack)
    try:
        local = await other.get(
            "/api/me", headers={"Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={local_id}"}
        )
        sso = await other.get(
            "/api/me", headers={"Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={oidc_id}"}
        )
    finally:
        await other.aclose()
    assert local.status_code == 401
    assert sso.status_code == 200


# --- review findings ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_flood_of_bogus_callbacks_is_throttled_and_does_not_fill_the_audit_log(
    make_stack: MakeStack,
) -> None:
    stack, _ = await sso_stack(make_stack)
    before = len(await audit_rows(stack))
    results = [error_code(await stack.client.get("/api/auth/oidc/callback")) for _ in range(130)]
    assert "rate_limited" in results[120:]
    assert len(await audit_rows(stack)) == before  # nothing was written per request


@pytest.mark.anyio
async def test_a_failure_after_a_valid_state_is_still_audited(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    response = await sign_in(stack, fake, sub=unique("s"), nonce="forged")
    assert error_code(response) == "sso_failed"
    assert "token:bad_nonce" in await failure_reasons(stack)


@pytest.mark.anyio
async def test_a_stripped_iss_parameter_is_refused_when_the_provider_advertises_it(
    make_stack: MakeStack,
) -> None:
    stack, fake = await sso_stack(make_stack)
    fake.discovery = {"authorization_response_iss_parameter_supported": True}
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    stripped = await stack.client.get("/api/auth/oidc/callback", params=params)
    assert error_code(stripped) == "sso_failed"
    assert "issuer_parameter_missing" in await failure_reasons(stack)
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    params["iss"] = ISSUER
    assert error_code(await stack.client.get("/api/auth/oidc/callback", params=params)) is None


@pytest.mark.anyio
async def test_free_text_from_the_browser_does_not_reach_the_audit_log(
    make_stack: MakeStack,
) -> None:
    stack, fake = await sso_stack(make_stack)
    started = await begin(stack.client)
    _, params = fake.authorize(started.headers["location"], sub=unique("s"))
    await stack.client.get(
        "/api/auth/oidc/callback",
        params={"error": "my-password-is-hunter2", "state": params["state"]},
    )
    reasons = await failure_reasons(stack)
    assert "provider_error:other" in reasons
    assert not any("hunter2" in reason for reason in reasons)


@pytest.mark.anyio
async def test_a_long_e_mail_and_no_name_still_signs_in(make_stack: MakeStack) -> None:
    stack, fake = await sso_stack(make_stack)
    subject = unique("s")
    response = await sign_in(stack, fake, sub=subject, name=None, email="b" * 300 + "@example.test")
    assert error_code(response) is None
    [user] = await user_rows(stack, subject)
    assert len(user["display_name"]) <= 200


@pytest.mark.anyio
@pytest.mark.parametrize(
    "target",
    [
        "%2F%2Fevil.example",
        "\uff0f\uff0fevil.example",  # full-width slashes
        "%5Cevil.example",
        "/\u2215evil.example",  # a division slash
        "‮/evil",
    ],
)
async def test_encoded_and_unicode_lookalikes_are_not_a_way_out(
    make_stack: MakeStack, target: str
) -> None:
    stack, fake = await sso_stack(make_stack)
    response = await sign_in(stack, fake, next_path=target, sub=unique("s"))
    location = response.headers["location"]
    assert location == "/" or (location.startswith("/") and not location.startswith("//"))
    assert "evil" not in urlsplit(location).netloc


@pytest.mark.anyio
async def test_nothing_secret_is_kept_in_redis_the_audit_log_or_the_session(
    make_stack: MakeStack,
) -> None:
    stack, fake = await sso_stack(make_stack)
    redis: Redis = stack.app.state.oidc._redis
    states_before = len([k async for k in redis.scan_iter("pharos:oidc:*")])
    response = await sign_in(stack, fake, sub=unique("s"))
    assert error_code(response) is None
    haystack = ""
    async for key in redis.scan_iter("pharos:*"):
        kind = await redis.type(key)
        if kind == b"string":
            haystack += (await redis.get(key) or b"").decode(errors="replace")
        elif kind == b"set":
            haystack += " ".join(m.decode() for m in await redis.smembers(key))
    haystack += str(await audit_rows(stack))
    for forbidden in (CLIENT_SECRET, "opaque-access-token", "code_verifier"):
        assert forbidden not in haystack
    # The stored login state is spent: nothing of the flow is left behind.
    assert len([k async for k in redis.scan_iter("pharos:oidc:*")]) == states_before
