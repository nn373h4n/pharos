"""Administrators managing people: list, change role, change status (M2-07)."""

# ruff: noqa: F811  (pytest fixtures imported from another test module are used by name)

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from pharos.core.sessions import SessionStoreUnavailable

from .test_local_auth import (  # noqa: F401  (fixtures and helpers used by these tests)
    PASSWORD,
    PUBLIC_URL,
    MakeStack,
    Stack,
    audit_rows,
    make_stack,
    make_user,
)

pytestmark = pytest.mark.integration


@dataclass
class Person:
    username: str
    id: uuid.UUID
    client: httpx.AsyncClient
    headers: dict[str, str]


async def person(stack: Stack, role: str, status: str = "active") -> Person:
    """A signed-in person with a client of their own (cookies are kept per client)."""
    username = await make_user(stack, role=role)
    async with stack.owner.connect() as connection:
        user_id = (
            await connection.execute(
                text("SELECT id FROM users WHERE subject = :name"), {"name": username}
            )
        ).scalar_one()
    octets = uuid.uuid4().bytes
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=stack.app, client=(f"10.{octets[0]}.{octets[1]}.{octets[2]}", 4000)
        ),
        base_url=PUBLIC_URL,
        headers={"Origin": PUBLIC_URL},
    )
    response = await client.post(
        "/api/auth/local/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    headers = {"X-CSRF-Token": response.json()["csrf_token"]}
    if status != "active":
        await set_in_db(stack, user_id, status=status)
    return Person(username, user_id, client, headers)


async def set_in_db(stack: Stack, user_id: uuid.UUID, **columns: str) -> None:
    assignments = ", ".join(f"{name} = :{name}" for name in columns)
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(f"UPDATE users SET {assignments} WHERE id = :id"),
            {"id": user_id, **columns},
        )


async def state_of(stack: Stack, user_id: uuid.UUID) -> tuple[str, str]:
    async with stack.owner.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT role, status FROM users WHERE id = :id"), {"id": user_id}
            )
        ).one()
    return row.role, row.status


async def other_organizations_user(stack: Stack) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with stack.owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, org_id, issuer, subject, display_name, role, status) "
                "VALUES (:id, :org, 'local', :subject, 'Someone Else', 'member', 'active')"
            ),
            {"id": user_id, "org": stack.org_b, "subject": f"beta-{user_id.hex[:8]}"},
        )
    return user_id


async def patch(who: Person, user_id: uuid.UUID, what: str, body: dict[str, Any]) -> httpx.Response:
    return await who.client.patch(f"/api/users/{user_id}/{what}", json=body, headers=who.headers)


# --- who may call ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_only_administrators_may_list_or_change_people(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    target = await person(stack, "member")
    outsiders = [await person(stack, "member"), await person(stack, "editor")]
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app, client=("10.1.2.3", 4000)),
        base_url=PUBLIC_URL,
        headers={"Origin": PUBLIC_URL},
    )
    try:
        assert (await anonymous.get("/api/users")).status_code == 401
        for who in outsiders:
            assert (await who.client.get("/api/users")).status_code == 403
            denied = await patch(who, target.id, "role", {"role": "admin"})
            assert denied.status_code == 403
            denied = await patch(who, target.id, "status", {"status": "disabled"})
            assert denied.status_code == 403
        assert await state_of(stack, target.id) == ("member", "active")
        assert (await admin.client.get("/api/users")).status_code == 200
    finally:
        await anonymous.aclose()


@pytest.mark.anyio
async def test_a_change_needs_the_csrf_token(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    target = await person(stack, "member")
    response = await admin.client.patch(f"/api/users/{target.id}/role", json={"role": "editor"})
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert await state_of(stack, target.id) == ("member", "active")


# --- listing -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_list_shows_people_with_role_and_status_and_pages(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    await person(stack, "member")
    await person(stack, "editor")
    body = (await admin.client.get("/api/users")).json()
    assert body["total"] == 3
    assert {u["role"] for u in body["items"]} == {"admin", "member", "editor"}
    first = body["items"][0]
    assert set(first) == {
        "id",
        "display_name",
        "email",
        "role",
        "status",
        "department",
        "last_login_at",
        "sign_in",
    }
    assert first["sign_in"] == "local"
    page = (await admin.client.get("/api/users", params={"limit": 1, "offset": 2})).json()
    assert page["total"] == 3
    assert [u["id"] for u in page["items"]] == [body["items"][2]["id"]]
    for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert (await admin.client.get("/api/users", params=bad)).status_code == 422


@pytest.mark.anyio
async def test_administrators_never_reach_another_organizations_people(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    foreign = await other_organizations_user(stack)
    listed = (await admin.client.get("/api/users")).json()
    assert str(foreign) not in {u["id"] for u in listed["items"]}
    assert listed["total"] == 1
    for what, body in (("role", {"role": "admin"}), ("status", {"status": "disabled"})):
        response = await patch(admin, foreign, what, body)
        assert response.status_code == 404  # same answer as for an id that does not exist
        assert response.json()["code"] == "not_found"
    assert await state_of(stack, foreign) == ("member", "active")


# --- changing a role ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_role_change_is_saved_audited_and_ends_the_targets_sessions(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    target = await person(stack, "member")
    assert (await target.client.get("/api/me")).status_code == 200

    response = await patch(admin, target.id, "role", {"role": "editor"})
    assert response.status_code == 200
    assert response.json()["role"] == "editor"
    assert await state_of(stack, target.id) == ("editor", "active")
    assert (await target.client.get("/api/me")).status_code == 401  # signed out everywhere

    rows = await audit_rows(stack)
    changed = [r for r in rows if r["action"] == "user.role_changed"]
    assert len(changed) == 1
    assert changed[0]["actor_id"] == admin.id
    assert changed[0]["target"] == f"user:{target.id}"
    assert changed[0]["details"] == {"from": "member", "to": "editor"}
    revoked = [r for r in rows if r["action"] == "auth.sessions_revoked"]
    assert len(revoked) == 1 and revoked[0]["details"] == {"removed": 1}


@pytest.mark.anyio
async def test_the_same_role_again_changes_nothing_and_writes_nothing(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    target = await person(stack, "member")
    before = len(await audit_rows(stack))
    response = await patch(admin, target.id, "role", {"role": "member"})
    assert response.status_code == 200
    assert len(await audit_rows(stack)) == before
    assert (await target.client.get("/api/me")).status_code == 200  # session untouched


@pytest.mark.anyio
async def test_a_role_changed_in_the_database_applies_on_the_next_request(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    other = await person(stack, "member")
    assert (await other.client.get("/api/users")).status_code == 403
    await set_in_db(stack, other.id, role="admin")
    assert (await other.client.get("/api/users")).status_code == 200  # no new sign-in needed
    await set_in_db(stack, admin.id, role="member")
    assert (await admin.client.get("/api/users")).status_code == 403


@pytest.mark.anyio
async def test_refused_changes_leave_a_failure_row(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    target = await person(stack, "member")
    missing = uuid.uuid4()
    await patch(admin, admin.id, "role", {"role": "member"})
    await patch(admin, missing, "status", {"status": "disabled"})
    await set_in_db(stack, target.id, status="deprovisioned")
    await patch(admin, target.id, "role", {"role": "admin"})
    failures = [
        (r["action"], r["target"], r["details"])
        for r in await audit_rows(stack)
        if r["outcome"] == "failure" and r["action"].startswith("user.")
    ]
    assert failures == [
        ("user.role_changed", f"user:{admin.id}", {"reason": "own_account"}),
        ("user.status_changed", f"user:{missing}", {"reason": "not_found"}),
        ("user.role_changed", f"user:{target.id}", {"reason": "status_locked"}),
    ]


@pytest.mark.anyio
async def test_nobody_changes_their_own_role_or_status(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    for what, body in (("role", {"role": "member"}), ("status", {"status": "disabled"})):
        response = await patch(admin, admin.id, what, body)
        assert response.status_code == 409
        assert response.json()["code"] == "own_account"
    assert await state_of(stack, admin.id) == ("admin", "active")


@pytest.mark.anyio
async def test_two_administrators_cannot_remove_each_other_at_the_same_moment(
    make_stack: MakeStack,
) -> None:
    for _ in range(5):  # a race: repeat so that a missing lock shows up
        stack: Stack = await make_stack("on")
        first = await person(stack, "admin")
        second = await person(stack, "admin")
        results = await asyncio.gather(
            patch(first, second.id, "role", {"role": "member"}),
            patch(second, first.id, "role", {"role": "member"}),
        )
        codes = sorted(r.status_code for r in results)
        # One wins. The other is refused: 403 if it got as far as the check, 401 if the winner
        # had already ended its session (it is the one that was removed).
        assert codes[0] == 200 and codes[1] in {401, 403}
        async with stack.owner.connect() as connection:
            admins = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM users WHERE id IN (:a, :b) "
                        "AND role = 'admin' AND status = 'active'"
                    ),
                    {"a": first.id, "b": second.id},
                )
            ).scalar_one()
        assert admins == 1  # the organization is never left without an administrator


# --- changing a status -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_disabling_ends_sessions_blocks_sign_in_and_enabling_restores_it(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    target = await person(stack, "member")

    response = await patch(admin, target.id, "status", {"status": "disabled"})
    assert response.status_code == 200 and response.json()["status"] == "disabled"
    assert (await target.client.get("/api/me")).status_code == 401
    refused = await target.client.post(
        "/api/auth/local/login", json={"username": target.username, "password": PASSWORD}
    )
    assert refused.status_code == 401

    response = await patch(admin, target.id, "status", {"status": "active"})
    assert response.status_code == 200 and response.json()["status"] == "active"
    again = await target.client.post(
        "/api/auth/local/login", json={"username": target.username, "password": PASSWORD}
    )
    assert again.status_code == 200

    actions = [
        r["details"] for r in await audit_rows(stack) if r["action"] == "user.status_changed"
    ]
    assert actions == [
        {"from": "active", "to": "disabled"},
        {"from": "disabled", "to": "active"},
    ]


@pytest.mark.anyio
async def test_a_user_the_identity_provider_removed_cannot_be_reactivated_here(
    make_stack: MakeStack,
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    target = await person(stack, "member")
    await set_in_db(stack, target.id, status="deprovisioned")
    response = await patch(admin, target.id, "status", {"status": "active"})
    assert response.status_code == 409
    assert response.json()["code"] == "status_locked"
    promoted = await patch(admin, target.id, "role", {"role": "admin"})
    assert promoted.status_code == 409  # rights are not handed out to a removed person either
    assert await state_of(stack, target.id) == ("member", "deprovisioned")


# --- bad input ---------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("what", "body"),
    [
        ("role", {"role": "owner"}),
        ("role", {"role": "curator"}),
        ("role", {}),
        ("role", {"role": "admin", "status": "active"}),
        ("status", {"status": "deprovisioned"}),
        ("status", {"status": "ACTIVE"}),
        ("status", {"status": "disabled", "role": "admin"}),
    ],
)
async def test_values_outside_the_allowed_sets_are_rejected(
    make_stack: MakeStack, what: str, body: dict[str, Any]
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    target = await person(stack, "member")
    response = await patch(admin, target.id, what, body)
    assert response.status_code == 422
    assert await state_of(stack, target.id) == ("member", "active")


@pytest.mark.anyio
async def test_unknown_and_malformed_ids(make_stack: MakeStack) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    missing = await patch(admin, uuid.uuid4(), "role", {"role": "editor"})
    assert missing.status_code == 404
    bad = await admin.client.patch(
        "/api/users/not-a-uuid/role", json={"role": "editor"}, headers=admin.headers
    )
    assert bad.status_code == 422


# --- when Redis cannot be reached --------------------------------------------------------------


@pytest.mark.anyio
async def test_the_database_decides_even_if_the_sessions_cannot_be_ended(
    make_stack: MakeStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack: Stack = await make_stack("on")
    admin = await person(stack, "admin")
    target = await person(stack, "member")

    async def down(user_id: uuid.UUID) -> int:
        raise SessionStoreUnavailable

    monkeypatch.setattr(stack.app.state.session_store, "revoke_user", down)
    response = await patch(admin, target.id, "status", {"status": "disabled"})
    assert response.status_code == 200
    assert await state_of(stack, target.id) == ("member", "disabled")
    # The old session still exists in Redis, but a disabled user is refused on every request.
    assert (await target.client.get("/api/me")).status_code == 401
    traced = [r for r in await audit_rows(stack) if r["action"] == "auth.sessions_revoked"]
    assert [(r["outcome"], r["details"]) for r in traced] == [
        ("failure", {"reason": "store_unavailable"})
    ]
