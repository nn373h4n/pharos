"""Organization resolution, the unit of work and the principal, against real PostgreSQL."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.auth import CurrentPrincipal, SessionIdentity, require_role
from pharos.core.config import Settings
from pharos.core.db import create_session_factory
from pharos.core.organization import OrganizationNotConfigured, OrganizationResolver
from pharos.core.principal import Principal, Role
from pharos.core.unit_of_work import UnitOfWork, UnitOfWorkDep
from pharos.main import create_app

from .conftest import Orgs

pytestmark = pytest.mark.integration


async def slug_of(engine: AsyncEngine, org_id: uuid.UUID) -> str:
    async with engine.connect() as connection:
        row = await connection.execute(
            text("SELECT slug FROM organizations WHERE id = :id"), {"id": org_id}
        )
        return str(row.scalar_one())


# --- organization resolution -------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_resolver_finds_the_organization_through_the_definer_function(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, orgs: Orgs
) -> None:
    alpha, beta = orgs
    slug_a, slug_b = await slug_of(owner_engine, alpha), await slug_of(owner_engine, beta)
    assert await OrganizationResolver(app_engine, slug_a).resolve() == alpha
    assert await OrganizationResolver(app_engine, slug_b).resolve() == beta


@pytest.mark.anyio
@pytest.mark.parametrize("slug", ["no-such-org", "", "' OR '1'='1", "alpha%", "ALPHA"])
async def test_an_unknown_slug_is_not_configured(app_engine: AsyncEngine, slug: str) -> None:
    with pytest.raises(OrganizationNotConfigured):
        await OrganizationResolver(app_engine, slug).resolve()


@pytest.mark.anyio
async def test_a_missing_organization_is_not_cached_so_the_operator_can_create_it_later(
    app_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    slug = f"late-{uuid.uuid4().hex[:8]}"
    resolver = OrganizationResolver(app_engine, slug)
    with pytest.raises(OrganizationNotConfigured):
        await resolver.resolve()
    async with owner_engine.begin() as connection:
        created = await connection.execute(
            text("INSERT INTO organizations (name, slug) VALUES ('Late', :slug) RETURNING id"),
            {"slug": slug},
        )
        org_id = created.scalar_one()
    try:
        assert await resolver.resolve() == org_id
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
            )


@pytest.mark.anyio
async def test_the_application_role_cannot_list_organizations(
    app_engine: AsyncEngine, orgs: Orgs
) -> None:
    # The lookup answers one question; the table itself stays closed to the application.
    async with app_engine.connect() as connection:
        count = (await connection.execute(text("SELECT count(*) FROM organizations"))).scalar_one()
    assert count == 0


# --- the application under test ----------------------------------------------------------------


@dataclass
class Plumbing:
    client: httpx.AsyncClient
    app: FastAPI
    engine: AsyncEngine
    owner: AsyncEngine
    org_a: uuid.UUID
    org_b: uuid.UUID


@pytest.fixture
async def plumbing(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, orgs: Orgs
) -> AsyncIterator[Plumbing]:
    org_a, org_b = orgs
    app = create_app(Settings(environment="test", local_login="on"))
    app.state.session_factory = create_session_factory(app_engine)
    app.state.organization = OrganizationResolver(app_engine, await slug_of(owner_engine, org_a))

    class HeaderSessions:
        """Test double for the session layer: the identity comes from two test headers."""

        async def resolve(self, request: Request) -> SessionIdentity | None:
            user = request.headers.get("x-test-user")
            if user is None:
                return None
            org = uuid.UUID(request.headers.get("x-test-org", str(org_a)))
            return SessionIdentity(user_id=uuid.UUID(user), org_id=org)

    app.state.sessions = HeaderSessions()

    @app.get("/api/_t/me")
    async def me(principal: CurrentPrincipal) -> dict[str, str]:
        return {"role": principal.role.value, "org": str(principal.org_id)}

    @app.get("/api/_t/admin")
    async def admin(
        principal: Principal = Depends(require_role(Role.ADMIN)),  # noqa: B008
    ) -> dict[str, str]:
        return {"user": str(principal.user_id)}

    @app.get("/api/_t/count-departments")
    async def count(uow: UnitOfWorkDep) -> dict[str, int]:
        rows = await uow.session.execute(text("SELECT count(*) FROM departments"))
        return {"count": rows.scalar_one()}

    async def add_department(uow: UnitOfWork, name: str) -> None:
        await uow.session.execute(
            text("INSERT INTO departments (org_id, name) VALUES (:org, :name)"),
            {"org": uow.org_id, "name": name},
        )

    @app.post("/api/_t/department/committed")
    async def committed(uow: UnitOfWorkDep) -> dict[str, str]:
        await add_department(uow, "committed")
        await uow.commit()
        return {"ok": "yes"}

    @app.post("/api/_t/department/forgotten-commit")
    async def forgotten(uow: UnitOfWorkDep) -> dict[str, str]:
        await add_department(uow, "forgotten")
        return {"ok": "yes"}

    @app.post("/api/_t/department/fails-after-write")
    async def fails(uow: UnitOfWorkDep) -> dict[str, str]:
        await add_department(uow, "fails")
        raise RuntimeError("boom")

    @app.post("/api/_t/department/commit-fails")
    async def commit_fails(uow: UnitOfWorkDep) -> dict[str, str]:
        await add_department(uow, "commit-fails")
        # A deferred constraint would fail at commit; a failing statement stands in for it.
        await uow.session.execute(text("SELECT 1/0"))
        await uow.commit()
        return {"ok": "yes"}

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    # The default PHAROS_PUBLIC_URL of development; state-changing requests must send it as Origin.
    origin = {"Origin": "http://localhost:3000"}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=origin
    ) as client:
        yield Plumbing(client, app, app_engine, owner_engine, org_a, org_b)
    async with owner_engine.begin() as connection:
        ids = {"ids": [org_a, org_b]}
        await connection.execute(text("DELETE FROM departments WHERE org_id = ANY(:ids)"), ids)
        await connection.execute(text("DELETE FROM users WHERE org_id = ANY(:ids)"), ids)


async def make_user(
    plumbing: Plumbing, org_id: uuid.UUID, *, role: str = "member", status: str = "active"
) -> uuid.UUID:
    async with plumbing.owner.begin() as connection:
        row = await connection.execute(
            text(
                "INSERT INTO users (org_id, issuer, subject, role, status) "
                "VALUES (:org, 'local', :sub, :role, :status) RETURNING id"
            ),
            {"org": org_id, "sub": uuid.uuid4().hex, "role": role, "status": status},
        )
        return row.scalar_one()


async def department_names(plumbing: Plumbing) -> set[str]:
    async with plumbing.owner.connect() as connection:
        rows = await connection.execute(
            text("SELECT name FROM departments WHERE org_id = ANY(:ids)"),
            {"ids": [plumbing.org_a, plumbing.org_b]},
        )
        return {row[0] for row in rows}


# --- unit of work ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_committed_write_is_stored_before_the_response(plumbing: Plumbing) -> None:
    response = await plumbing.client.post("/api/_t/department/committed")
    assert response.status_code == 200
    assert await department_names(plumbing) == {"committed"}


@pytest.mark.anyio
async def test_a_write_that_is_never_committed_is_rolled_back(plumbing: Plumbing) -> None:
    response = await plumbing.client.post("/api/_t/department/forgotten-commit")
    assert response.status_code == 200
    assert await department_names(plumbing) == set()


@pytest.mark.anyio
async def test_a_failing_request_rolls_back_and_answers_500(plumbing: Plumbing) -> None:
    response = await plumbing.client.post("/api/_t/department/fails-after-write")
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert await department_names(plumbing) == set()


@pytest.mark.anyio
async def test_a_failing_commit_is_never_reported_as_success(plumbing: Plumbing) -> None:
    response = await plumbing.client.post("/api/_t/department/commit-fails")
    assert response.status_code == 500
    assert await department_names(plumbing) == set()


@pytest.mark.anyio
async def test_a_request_only_sees_its_own_organization(plumbing: Plumbing) -> None:
    rows = [
        (plumbing.org_a, "mine"),
        (plumbing.org_b, "theirs-1"),
        (plumbing.org_b, "theirs-2"),
    ]
    async with plumbing.owner.begin() as connection:
        for org_id, name in rows:
            await connection.execute(
                text("INSERT INTO departments (org_id, name) VALUES (:org, :name)"),
                {"org": org_id, "name": name},
            )
    response = await plumbing.client.get("/api/_t/count-departments")
    assert response.json() == {"count": 1}


@pytest.mark.anyio
async def test_requests_fail_with_503_until_the_organization_exists(plumbing: Plumbing) -> None:
    plumbing.app.state.organization = OrganizationResolver(plumbing.engine, "does-not-exist")
    response = await plumbing.client.get("/api/_t/count-departments")
    assert response.status_code == 503
    assert response.json()["code"] == "organization_not_configured"


# --- principal ---------------------------------------------------------------------------------


def as_user(user: uuid.UUID, org: uuid.UUID | None = None) -> dict[str, str]:
    headers = {"x-test-user": str(user)}
    if org is not None:
        headers["x-test-org"] = str(org)
    return headers


@pytest.mark.anyio
async def test_the_principal_comes_from_the_user_row(plumbing: Plumbing) -> None:
    admin = await make_user(plumbing, plumbing.org_a, role="admin")
    response = await plumbing.client.get("/api/_t/me", headers=as_user(admin))
    assert response.status_code == 200
    assert response.json() == {"role": "admin", "org": str(plumbing.org_a)}


@pytest.mark.anyio
async def test_no_session_means_401(plumbing: Plumbing) -> None:
    response = await plumbing.client.get("/api/_t/me")
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


@pytest.mark.anyio
async def test_a_role_change_applies_on_the_next_request(plumbing: Plumbing) -> None:
    user = await make_user(plumbing, plumbing.org_a, role="admin")
    assert (await plumbing.client.get("/api/_t/admin", headers=as_user(user))).status_code == 200
    async with plumbing.owner.begin() as connection:
        await connection.execute(
            text("UPDATE users SET role = 'member' WHERE id = :id"), {"id": user}
        )
    demoted = await plumbing.client.get("/api/_t/admin", headers=as_user(user))
    assert demoted.status_code == 403
    assert demoted.json()["code"] == "forbidden"
    now = await plumbing.client.get("/api/_t/me", headers=as_user(user))
    assert now.json()["role"] == "member"


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["disabled", "deprovisioned"])
async def test_an_inactive_user_is_refused_with_the_same_answer_as_no_session(
    plumbing: Plumbing, status: str
) -> None:
    user = await make_user(plumbing, plumbing.org_a, role="admin", status=status)
    refused = await plumbing.client.get("/api/_t/me", headers=as_user(user))
    anonymous = await plumbing.client.get("/api/_t/me")
    assert refused.status_code == 401
    assert refused.json()["code"] == anonymous.json()["code"] == "not_authenticated"


@pytest.mark.anyio
async def test_a_user_that_does_not_exist_is_refused(plumbing: Plumbing) -> None:
    response = await plumbing.client.get("/api/_t/me", headers=as_user(uuid.uuid4()))
    assert response.status_code == 401


@pytest.mark.anyio
async def test_a_session_for_another_organization_is_refused(plumbing: Plumbing) -> None:
    stranger = await make_user(plumbing, plumbing.org_b, role="admin")
    # The session claims organization B while the installation is organization A.
    wrong_org = await plumbing.client.get("/api/_t/me", headers=as_user(stranger, plumbing.org_b))
    assert wrong_org.status_code == 401
    # The session claims A but names B's user: row-level security hides the row.
    wrong_user = await plumbing.client.get("/api/_t/me", headers=as_user(stranger, plumbing.org_a))
    assert wrong_user.status_code == 401


@pytest.mark.anyio
async def test_a_member_cannot_use_an_admin_route(plumbing: Plumbing) -> None:
    member = await make_user(plumbing, plumbing.org_a)
    response = await plumbing.client.get("/api/_t/admin", headers=as_user(member))
    assert response.status_code == 403


# --- the connection is only held while it is used -----------------------------------------------


@pytest.mark.anyio
async def test_a_request_holds_no_connection_until_it_uses_the_database(
    plumbing: Plumbing,
) -> None:
    pool = plumbing.engine.pool
    seen: dict[str, int] = {}

    @plumbing.app.get("/api/_t/pool")
    async def pool_state(uow: UnitOfWorkDep) -> dict[str, int]:
        seen["before_use"] = pool.checkedout()  # type: ignore[attr-defined]
        await uow.session.execute(text("SELECT 1"))
        seen["while_used"] = pool.checkedout()  # type: ignore[attr-defined]
        await uow.release()
        seen["after_release"] = pool.checkedout()  # type: ignore[attr-defined]
        return {}

    response = await plumbing.client.get("/api/_t/pool")
    assert response.status_code == 200
    assert seen == {"before_use": 0, "while_used": 1, "after_release": 0}
    assert pool.checkedout() == 0  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_the_organization_still_applies_after_a_commit_and_after_a_release(
    plumbing: Plumbing,
) -> None:
    async with plumbing.owner.begin() as connection:
        for org_id, name in ((plumbing.org_a, "mine"), (plumbing.org_b, "theirs")):
            await connection.execute(
                text("INSERT INTO departments (org_id, name) VALUES (:org, :name)"),
                {"org": org_id, "name": name},
            )
    counts: dict[str, int] = {}

    @plumbing.app.get("/api/_t/scope-after")
    async def scope_after(uow: UnitOfWorkDep) -> dict[str, int]:
        async def count() -> int:
            rows = await uow.session.execute(text("SELECT count(*) FROM departments"))
            return int(rows.scalar_one())

        counts["first"] = await count()
        await uow.commit()
        counts["after_commit"] = await count()  # a new transaction: scoped again
        await uow.release()
        counts["after_release"] = await count()
        return {}

    assert (await plumbing.client.get("/api/_t/scope-after")).status_code == 200
    assert counts == {"first": 1, "after_commit": 1, "after_release": 1}


@pytest.mark.anyio
async def test_a_second_request_never_inherits_the_first_ones_organization(
    plumbing: Plumbing,
) -> None:
    # The pool reuses connections: the setting must not leak from one transaction into the next.
    async with plumbing.engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :o, false)"), {"o": str(plumbing.org_b)}
        )
    count = await plumbing.client.get("/api/_t/count-departments")
    assert count.json() == {"count": 0}
