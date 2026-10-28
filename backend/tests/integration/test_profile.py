"""Profile, onboarding, professions and departments through the real application."""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from pharos.core.csrf import HOST_PREFIXED_SESSION_COOKIE
from pharos.profile.catalog import INTEREST_CODES

from .test_local_auth import (  # noqa: F401  (fixtures and helpers used by these tests)
    PUBLIC_URL,
    MakeStack,
    Stack,
    login,
    make_stack,
    make_user,
    unique,
)

pytestmark = pytest.mark.integration


async def sign_in(stack: Stack, *, role: str = "member") -> tuple[str, dict[str, str]]:
    """A signed-in member (the stack allows every local account) and the headers to use."""
    username = await make_user(stack, role=role)
    response = await login(stack, username)
    assert response.status_code == 200, response.text
    return username, {"X-CSRF-Token": response.json()["csrf_token"]}


async def a_profession(stack: Stack) -> str:
    return str((await stack.client.get("/api/professions")).json()[0]["id"])


# --- reading -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_new_account_has_an_empty_profile(make_stack: MakeStack) -> None:
    stack = await make_stack("on")
    await sign_in(stack)
    profile = (await stack.client.get("/api/me/profile")).json()
    assert profile == {
        "profession_id": None,
        "ai_level": None,
        "interests": [],
        "department_id": None,
        "locale": None,
        "effective_locale": "en",
        "onboarding_started_at": None,
        "onboarding_completed_at": None,
    }
    me = (await stack.client.get("/api/me")).json()
    assert me["user"]["onboarding_completed_at"] is None


@pytest.mark.anyio
async def test_every_profile_route_needs_a_session(make_stack: MakeStack) -> None:
    stack = await make_stack("on")
    for method, path in [
        ("GET", "/api/me/profile"),
        ("PUT", "/api/me/profile"),
        ("POST", "/api/me/onboarding/complete"),
        ("GET", "/api/me/profile/options"),
        ("GET", "/api/professions"),
        ("GET", "/api/departments"),
    ]:
        response = await stack.client.request(method, path, json={} if method == "PUT" else None)
        assert response.status_code == 401, (method, path)
        assert response.json()["code"] == "not_authenticated"


@pytest.mark.anyio
async def test_the_options_are_the_fixed_catalogue(make_stack: MakeStack) -> None:
    stack = await make_stack("on")
    await sign_in(stack)
    options = (await stack.client.get("/api/me/profile/options")).json()
    assert options == {
        "ai_levels": ["beginner", "intermediate", "advanced"],
        "interests": list(INTEREST_CODES),
        "locales": ["en", "az", "tr", "ru"],
    }


@pytest.mark.anyio
async def test_professions_come_with_all_four_languages(make_stack: MakeStack) -> None:
    stack = await make_stack("on")
    await sign_in(stack)
    professions = (await stack.client.get("/api/professions")).json()
    assert len(professions) >= 10
    for profession in professions:
        assert set(profession["names"]) == {"en", "az", "tr", "ru"}
        assert all(profession["names"].values())
    assert [p["names"]["en"] for p in professions] == sorted(p["names"]["en"] for p in professions)


# --- writing -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_profile_is_built_step_by_step(make_stack: MakeStack) -> None:
    stack = await make_stack("on")
    _, headers = await sign_in(stack)
    profession = await a_profession(stack)

    first = (
        await stack.client.put(
            "/api/me/profile", json={"profession_id": profession}, headers=headers
        )
    ).json()
    assert first["profession_id"] == profession
    assert first["onboarding_started_at"] is not None
    started = first["onboarding_started_at"]

    second = (
        await stack.client.put(
            "/api/me/profile",
            json={"interests": ["coding", "writing", "coding"][:2], "ai_level": "advanced"},
            headers=headers,
        )
    ).json()
    assert second["profession_id"] == profession  # untouched by the later step
    assert second["interests"] == ["writing", "coding"]  # catalogue order
    assert second["ai_level"] == "advanced"
    assert second["onboarding_started_at"] == started  # set once

    cleared = (
        await stack.client.put("/api/me/profile", json={"ai_level": None}, headers=headers)
    ).json()
    assert cleared["ai_level"] is None
    assert cleared["interests"] == ["writing", "coding"]


@pytest.mark.anyio
async def test_the_language_choice_is_saved_on_the_user(make_stack: MakeStack) -> None:
    stack = await make_stack("on")
    username, headers = await sign_in(stack)
    response = await stack.client.put("/api/me/profile", json={"locale": "az"}, headers=headers)
    assert response.json()["locale"] == "az"
    assert response.json()["effective_locale"] == "az"
    async with stack.owner.connect() as connection:
        stored = (
            await connection.execute(
                text("SELECT locale FROM users WHERE subject = :s"), {"s": username}
            )
        ).scalar_one()
    assert stored == "az"


@pytest.mark.anyio
async def test_the_effective_language_follows_the_precedence(make_stack: MakeStack) -> None:
    stack = await make_stack("on", default_locale="ru")
    _, headers = await sign_in(stack)
    nothing = await stack.client.get("/api/me/profile", headers={"Accept-Language": "de"})
    assert nothing.json()["effective_locale"] == "ru"  # installation default
    browser = await stack.client.get(
        "/api/me/profile", headers={"Accept-Language": "de, tr;q=0.8, en;q=0.4"}
    )
    assert browser.json()["effective_locale"] == "tr"  # the browser's best supported language
    await stack.client.put("/api/me/profile", json={"locale": "az"}, headers=headers)
    chosen = await stack.client.get("/api/me/profile", headers={"Accept-Language": "tr"})
    assert chosen.json()["effective_locale"] == "az"  # the person's own choice wins


@pytest.mark.anyio
async def test_a_department_choice_is_marked_as_the_users_own(make_stack: MakeStack) -> None:
    stack = await make_stack("on")
    username, headers = await sign_in(stack)
    async with stack.owner.begin() as connection:
        department = (
            await connection.execute(
                text("INSERT INTO departments (org_id, name) VALUES (:o, 'Finance') RETURNING id"),
                {"o": stack.org_a},
            )
        ).scalar_one()
    listed = (await stack.client.get("/api/departments")).json()
    assert {"id": str(department), "name": "Finance"} in listed

    chosen = await stack.client.put(
        "/api/me/profile", json={"department_id": str(department)}, headers=headers
    )
    assert chosen.json()["department_id"] == str(department)
    me = (await stack.client.get("/api/me")).json()
    assert me["user"]["department"] == "Finance"

    async def source() -> str | None:
        async with stack.owner.connect() as connection:
            return (
                await connection.execute(
                    text("SELECT department_source FROM users WHERE subject = :s"), {"s": username}
                )
            ).scalar_one()

    assert await source() == "user"  # a later identity-provider sign-in must not overwrite it
    await stack.client.put("/api/me/profile", json={"department_id": None}, headers=headers)
    assert await source() is None


# --- refusing bad input ------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"profession_id": str(uuid.uuid4())}, "profession_id"),
        ({"ai_level": "expert"}, "ai_level"),
        ({"ai_level": "BEGINNER"}, "ai_level"),
        ({"interests": ["not-a-topic"]}, "interests"),
        ({"interests": ["writing", "writing"]}, "interests"),
        ({"interests": [*INTEREST_CODES, "writing"]}, "interests"),
        ({"interests": "writing"}, "interests"),
        ({"interests": [1, 2]}, "interests"),
        ({"locale": "de"}, "locale"),
        ({"locale": "EN"}, "locale"),
        ({"department_id": str(uuid.uuid4())}, "department_id"),
        ({"profession_id": "not-a-uuid"}, "profession_id"),
    ],
)
async def test_unknown_or_malformed_choices_are_refused_and_name_the_field(
    make_stack: MakeStack, body: dict[str, Any], field: str
) -> None:
    stack = await make_stack("on")
    _, headers = await sign_in(stack)
    response = await stack.client.put("/api/me/profile", json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert response.json()["errors"][0]["loc"][:2] == ["body", field]
    assert (await stack.client.get("/api/me/profile")).json()[
        "profession_id"
    ] is None  # nothing saved


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field", ["org_id", "user_id", "role", "onboarding_completed_at", "status"]
)
async def test_fields_that_are_not_the_persons_to_set_are_rejected(
    make_stack: MakeStack, field: str
) -> None:
    stack = await make_stack("on")
    _, headers = await sign_in(stack)
    response = await stack.client.put(
        "/api/me/profile", json={field: str(uuid.uuid4())}, headers=headers
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


@pytest.mark.anyio
async def test_a_refused_request_does_not_echo_what_was_sent(make_stack: MakeStack) -> None:
    stack = await make_stack("on")
    _, headers = await sign_in(stack)
    response = await stack.client.put(
        "/api/me/profile", json={"interests": ["leak-canary-12345"]}, headers=headers
    )
    assert "leak-canary-12345" not in response.text


@pytest.mark.anyio
async def test_changing_the_profile_needs_the_csrf_token(make_stack: MakeStack) -> None:
    stack = await make_stack("on")
    await sign_in(stack)
    response = await stack.client.put("/api/me/profile", json={"locale": "az"})
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert (await stack.client.get("/api/me/profile")).json()["locale"] is None


# --- finishing onboarding ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_onboarding_cannot_be_completed_with_answers_missing(make_stack: MakeStack) -> None:
    stack = await make_stack("on")
    _, headers = await sign_in(stack)
    nothing = await stack.client.post("/api/me/onboarding/complete", headers=headers)
    assert nothing.status_code == 409
    assert nothing.json()["code"] == "onboarding_incomplete"
    assert set(nothing.json()["missing"]) == {"profession_id", "interests", "locale", "ai_level"}

    await stack.client.put(
        "/api/me/profile",
        json={"profession_id": await a_profession(stack), "locale": "tr"},
        headers=headers,
    )
    partial = await stack.client.post("/api/me/onboarding/complete", headers=headers)
    assert set(partial.json()["missing"]) == {"interests", "ai_level"}
    me = (await stack.client.get("/api/me")).json()
    assert me["user"]["onboarding_completed_at"] is None


@pytest.mark.anyio
async def test_completing_onboarding_is_recorded_once(make_stack: MakeStack) -> None:
    stack = await make_stack("on")
    _, headers = await sign_in(stack)
    await stack.client.put(
        "/api/me/profile",
        json={
            "profession_id": await a_profession(stack),
            "interests": ["reporting"],
            "locale": "ru",
            "ai_level": "beginner",
        },
        headers=headers,
    )
    done = await stack.client.post("/api/me/onboarding/complete", headers=headers)
    assert done.status_code == 200
    completed = done.json()["onboarding_completed_at"]
    started = done.json()["onboarding_started_at"]
    assert completed and started and started <= completed  # the time people need can be measured

    again = await stack.client.post("/api/me/onboarding/complete", headers=headers)
    assert again.status_code == 200
    assert again.json()["onboarding_completed_at"] == completed  # not stamped a second time
    me = (await stack.client.get("/api/me")).json()
    assert me["user"]["onboarding_completed_at"] == completed

    # Changing answers later is allowed and keeps both timestamps.
    changed = await stack.client.put(
        "/api/me/profile", json={"ai_level": "advanced"}, headers=headers
    )
    assert changed.json()["onboarding_completed_at"] == completed
    assert changed.json()["onboarding_started_at"] == started


# --- tenants -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_one_organization_never_sees_anothers_profiles(make_stack: MakeStack) -> None:
    stack = await make_stack("on")
    _, headers = await sign_in(stack)

    # Organization B has its own profession, a department and a user with a profile.
    async with stack.owner.begin() as connection:
        foreign_profession = (
            await connection.execute(
                text(
                    "INSERT INTO professions (scope, owner_org_id, code, names) VALUES "
                    "('org', :o, :code, CAST(:names AS jsonb)) RETURNING id"
                ),
                {
                    "o": stack.org_b,
                    "code": unique("secretjob").replace("-", "_"),
                    "names": '{"en":"Secret job","az":"a","tr":"b","ru":"c"}',
                },
            )
        ).scalar_one()
        foreign_department = (
            await connection.execute(
                text(
                    "INSERT INTO departments (org_id, name) VALUES (:o, 'Secret dept') RETURNING id"
                ),
                {"o": stack.org_b},
            )
        ).scalar_one()
        foreign_user = (
            await connection.execute(
                text(
                    "INSERT INTO users (org_id, issuer, subject) VALUES (:o, 'local', :s) RETURNING id"
                ),
                {"o": stack.org_b, "s": unique("theirs")},
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO user_profiles (user_id, org_id, profession_id, ai_level, interests) "
                "VALUES (:u, :o, :p, 'advanced', '[\"coding\"]')"
            ),
            {"u": foreign_user, "o": stack.org_b, "p": foreign_profession},
        )

    # Their rows are invisible: not listed, and not accepted as choices.
    professions = (await stack.client.get("/api/professions")).json()
    assert str(foreign_profession) not in str(professions)
    assert "Secret dept" not in (await stack.client.get("/api/departments")).text
    for body in (
        {"profession_id": str(foreign_profession)},
        {"department_id": str(foreign_department)},
    ):
        response = await stack.client.put("/api/me/profile", json=body, headers=headers)
        assert response.status_code == 422

    # A session forged for their user cannot read or change their profile through this installation.
    session_id, record = await stack.app.state.session_store.create(foreign_user, stack.org_b)
    other = httpx.AsyncClient(transport=httpx.ASGITransport(app=stack.app), base_url=PUBLIC_URL)
    try:
        cookie = {"Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={session_id}"}
        assert (await other.get("/api/me/profile", headers=cookie)).status_code == 401
        own_org_claim = await stack.app.state.session_store.create(foreign_user, stack.org_a)
        lying = {"Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={own_org_claim[0]}"}
        assert (await other.get("/api/me/profile", headers=lying)).status_code == 401

        # The state-changing routes too, with a valid Origin and a valid token for the forged
        # session, so only the tenant check can refuse them.
        from pharos.core.csrf import csrf_token

        for forged_id, forged_record in ((session_id, record), own_org_claim):
            headers = {
                "Cookie": f"{HOST_PREFIXED_SESSION_COOKIE}={forged_id}",
                "Origin": PUBLIC_URL,
                "X-CSRF-Token": csrf_token(stack.app.state.csrf_key, forged_id, forged_record),
            }
            complete = await other.post("/api/me/onboarding/complete", headers=headers)
            change = await other.put("/api/me/profile", json={"locale": "az"}, headers=headers)
            assert (complete.status_code, change.status_code) == (401, 401)
    finally:
        await other.aclose()
    async with stack.owner.connect() as connection:  # and nothing of theirs was changed
        locale = (
            await connection.execute(
                text("SELECT locale FROM users WHERE id = :id"), {"id": foreign_user}
            )
        ).scalar_one()
    assert locale is None

    # And our own profile is unaffected by theirs.
    assert (await stack.client.get("/api/me/profile")).json()["profession_id"] is None


@pytest.mark.anyio
async def test_the_organizations_own_professions_are_offered_to_its_members(
    make_stack: MakeStack,
) -> None:
    stack = await make_stack("on")
    _, headers = await sign_in(stack)
    async with stack.owner.begin() as connection:
        own = (
            await connection.execute(
                text(
                    "INSERT INTO professions (scope, owner_org_id, code, names) VALUES "
                    "('org', :o, :code, CAST(:names AS jsonb)) RETURNING id"
                ),
                {
                    "o": stack.org_a,
                    "code": unique("ownjob").replace("-", "_"),
                    "names": '{"en":"Zzz own job","az":"a","tr":"b","ru":"c"}',
                },
            )
        ).scalar_one()
    listed = (await stack.client.get("/api/professions")).json()
    assert str(own) in {p["id"] for p in listed}
    chosen = await stack.client.put(
        "/api/me/profile", json={"profession_id": str(own)}, headers=headers
    )
    assert chosen.status_code == 200
    assert chosen.json()["profession_id"] == str(own)


@pytest.mark.anyio
async def test_the_queries_filter_by_organization_even_without_row_level_security(
    make_stack: MakeStack,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pharos.profile import repository

    stack = await make_stack("on")
    async with stack.owner.begin() as connection:
        theirs = (
            await connection.execute(
                text(
                    "INSERT INTO professions (scope, owner_org_id, code, names) VALUES "
                    "('org', :o, :code, CAST(:names AS jsonb)) RETURNING id"
                ),
                {
                    "o": stack.org_b,
                    "code": unique("hidden").replace("-", "_"),
                    "names": '{"en":"Hidden","az":"a","tr":"b","ru":"c"}',
                },
            )
        ).scalar_one()
        department = (
            await connection.execute(
                text("INSERT INTO departments (org_id, name) VALUES (:o, 'Theirs') RETURNING id"),
                {"o": stack.org_b},
            )
        ).scalar_one()
    # The owner role bypasses row-level security, so only the explicit filters can answer.
    async with AsyncSession(stack.owner) as session:
        assert not await repository.profession_exists(session, stack.org_a, theirs)
        assert await repository.profession_exists(session, stack.org_b, theirs)
        assert theirs not in {p.id for p in await repository.list_professions(session, stack.org_a)}
        assert theirs in {p.id for p in await repository.list_professions(session, stack.org_b)}
        assert not await repository.department_exists(session, stack.org_a, department)
        assert department not in {
            d.id for d in await repository.list_departments(session, stack.org_a)
        }
