"""The audit log service against real PostgreSQL."""

import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.audit import ActorType, AuditAction, AuditLog, Outcome
from pharos.core.config import Settings
from pharos.core.db import create_session_factory
from pharos.core.organization import OrganizationResolver
from pharos.core.rls import tenant_session
from pharos.core.unit_of_work import UnitOfWorkDep
from pharos.main import create_app

from ..constants import FAKE_SECRET_KEY
from .conftest import Orgs
from .test_request_plumbing import slug_of

pytestmark = pytest.mark.integration

IP_KEY = b"k" * 32


async def rows(owner: AsyncEngine, org_id: uuid.UUID) -> list[dict[str, object]]:
    async with owner.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT actor_type, actor_id, action, target, outcome, ip_hash, details "
                "FROM audit_logs WHERE org_id = :org ORDER BY id"
            ),
            {"org": org_id},
        )
        return [dict(row._mapping) for row in result]


@pytest.fixture
def audit(app_engine: AsyncEngine) -> AuditLog:
    return AuditLog(create_session_factory(app_engine), IP_KEY)


@pytest.mark.anyio
async def test_a_record_commits_with_the_callers_transaction(
    audit: AuditLog, app_engine: AsyncEngine, owner_engine: AsyncEngine, orgs: Orgs
) -> None:
    org, _ = orgs
    actor = uuid.uuid4()
    async with tenant_session(create_session_factory(app_engine), org) as session:
        await audit.record(
            session,
            org_id=org,
            action=AuditAction.USER_ROLE_CHANGED,
            actor_type=ActorType.LOCAL_ADMIN,
            actor_id=actor,
            target=f"user:{uuid.uuid4()}",
            ip="203.0.113.9",
            details={"old_role": "member", "new_role": "editor"},
        )
    [row] = await rows(owner_engine, org)
    assert row["action"] == "user.role_changed"
    assert row["outcome"] == "success"
    assert row["actor_type"] == "local_admin"
    assert row["actor_id"] == actor
    assert row["details"] == {"old_role": "member", "new_role": "editor"}
    assert row["ip_hash"] is not None
    assert "203.0.113.9" not in str(row)


@pytest.mark.anyio
async def test_a_record_is_rolled_back_with_the_callers_transaction(
    audit: AuditLog, app_engine: AsyncEngine, owner_engine: AsyncEngine, orgs: Orgs
) -> None:
    org, _ = orgs
    with pytest.raises(RuntimeError):
        async with tenant_session(create_session_factory(app_engine), org) as session:
            await audit.record(session, org_id=org, action=AuditAction.LOGOUT)
            raise RuntimeError("the business change failed")
    assert await rows(owner_engine, org) == []


@pytest.mark.anyio
async def test_an_independent_record_survives_a_failing_request(
    audit: AuditLog, app_engine: AsyncEngine, owner_engine: AsyncEngine, orgs: Orgs
) -> None:
    org, other = orgs
    app: FastAPI = create_app(Settings(environment="test", secret_key=FAKE_SECRET_KEY))  # type: ignore[arg-type]
    app.state.session_factory = create_session_factory(app_engine)
    app.state.organization = OrganizationResolver(app_engine, await slug_of(owner_engine, org))

    @app.post("/api/_t/login-attempt")
    async def attempt(uow: UnitOfWorkDep) -> None:
        await audit.record_independently(
            org_id=uow.org_id,
            action=AuditAction.LOGIN,
            outcome=Outcome.FAILURE,
            target="local:alice",
            ip="198.51.100.5",
            details={"reason": "bad_password"},
        )
        raise RuntimeError("the request fails and its own transaction rolls back")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Origin": "http://localhost:3000"}
    ) as client:
        response = await client.post("/api/_t/login-attempt")
    assert response.status_code == 500
    [row] = await rows(owner_engine, org)
    assert (row["action"], row["outcome"], row["target"]) == (
        "auth.login",
        "failure",
        "local:alice",
    )
    assert await rows(owner_engine, other) == []


@pytest.mark.anyio
async def test_a_failed_audit_write_is_reported_not_raised(
    app_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    audit = AuditLog(create_session_factory(app_engine), IP_KEY)
    # An organization that does not exist: the insert violates the foreign key.
    written = await audit.record_independently(org_id=uuid.uuid4(), action=AuditAction.LOGIN)
    assert written is False
    assert any("could not write the audit row" in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_rows_are_scoped_to_their_organization(
    audit: AuditLog, app_engine: AsyncEngine, owner_engine: AsyncEngine, orgs: Orgs
) -> None:
    org_a, org_b = orgs
    assert await audit.record_independently(org_id=org_a, action=AuditAction.LOGIN)
    assert await audit.record_independently(org_id=org_b, action=AuditAction.LOGOUT)
    assert [r["action"] for r in await rows(owner_engine, org_a)] == ["auth.login"]
    assert [r["action"] for r in await rows(owner_engine, org_b)] == ["auth.logout"]
    # A session scoped to A cannot read or write B's log.
    async with tenant_session(create_session_factory(app_engine), org_a) as session:
        visible = (await session.execute(text("SELECT action FROM audit_logs"))).scalars().all()
    assert visible == ["auth.login"]
    with pytest.raises(DBAPIError):
        async with tenant_session(create_session_factory(app_engine), org_a) as session:
            await audit.record(session, org_id=org_b, action=AuditAction.LOGOUT)


@pytest.mark.anyio
async def test_the_application_cannot_rewrite_or_erase_history(
    audit: AuditLog, app_engine: AsyncEngine, orgs: Orgs
) -> None:
    org, _ = orgs
    assert await audit.record_independently(org_id=org, action=AuditAction.LOGIN)
    for statement in ("UPDATE audit_logs SET outcome = 'success'", "DELETE FROM audit_logs"):
        with pytest.raises(DBAPIError):
            async with tenant_session(create_session_factory(app_engine), org) as session:
                await session.execute(text(statement))


@pytest.mark.anyio
async def test_secret_looking_details_never_reach_the_database(
    audit: AuditLog, owner_engine: AsyncEngine, orgs: Orgs
) -> None:
    org, _ = orgs
    with pytest.raises(ValueError, match="secret"):
        await audit.record_independently(
            org_id=org, action=AuditAction.LOGIN, details={"password": "hunter2"}
        )
    assert await rows(owner_engine, org) == []


@pytest.mark.anyio
async def test_long_targets_are_shortened_to_fit(
    audit: AuditLog, owner_engine: AsyncEngine, orgs: Orgs
) -> None:
    org, _ = orgs
    assert await audit.record_independently(
        org_id=org, action=AuditAction.LOGIN, target="x" * 5000, details={"note": "y" * 5000}
    )
    [row] = await rows(owner_engine, org)
    assert len(str(row["target"])) == 200
    assert len(str(row["details"]["note"])) == 200  # type: ignore[index]
