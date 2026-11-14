"""The worker writes the two budget events into the audit log, and nothing else (F-39, 0013)."""

import asyncio
import uuid
from typing import Any

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.llm.events import DatabaseBudgetEvents

from .conftest import TestDatabase
from .ingestion_support import Org

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def rows(owner_engine: AsyncEngine, org: Org) -> list[Any]:
    async with owner_engine.connect() as connection:
        found = await connection.execute(
            text(
                "SELECT actor_type, actor_id, action, target, outcome, ip_hash, details "
                "FROM audit_logs WHERE org_id = :o AND action LIKE 'llm.budget%' ORDER BY id"
            ),
            {"o": org.id},
        )
        return list(found.all())


async def test_both_budget_events_are_written_as_the_system_naming_only_the_organization(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    events = DatabaseBudgetEvents(worker_engine)
    await events("llm.budget_warning", alpha.id)
    await events("llm.budget_exhausted", alpha.id)
    found = await rows(owner_engine, alpha)
    assert [(r.action, r.actor_type, r.outcome) for r in found] == [
        ("llm.budget_warning", "system", "success"),
        ("llm.budget_exhausted", "system", "success"),
    ]
    assert all(r.actor_id is None and r.target is None and r.ip_hash is None for r in found)
    assert all(r.details == {} for r in found)
    assert await rows(owner_engine, beta) == []  # only the organization it was for
    with pytest.raises(ValueError, match="not a budget event"):
        await events("user.role_changed", alpha.id)


async def insert_as_worker(worker_engine: AsyncEngine, org: Org, columns: dict[str, Any]) -> None:
    names = ", ".join(columns)
    marks = ", ".join(f":{n}" for n in columns)
    async with worker_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org.id)}
        )
        await connection.execute(
            text(f"INSERT INTO audit_logs ({names}) VALUES ({marks})"),
            columns,
        )


BUDGET = {"actor_type": "system", "action": "llm.budget_warning", "outcome": "success"}


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "user.role_changed"},  # another action
        {"action": "llm.key_test_requested"},
        {"actor_type": "user"},
        {"outcome": "failure"},
        {"actor_id": str(uuid.uuid4())},  # a person named as the actor
        {"target": "user:someone"},
        {"ip_hash": "ab" * 32},
        {"details": '{"note": "forged"}'},
    ],
)
async def test_the_worker_cannot_write_any_other_audit_entry(
    worker_engine: AsyncEngine, owner_engine: AsyncEngine, alpha: Org, changes: dict[str, Any]
) -> None:
    with pytest.raises(DBAPIError) as refused:
        await insert_as_worker(worker_engine, alpha, {"org_id": alpha.id, **BUDGET, **changes})
    # A column it has no right to (permission denied) or a row the trigger refuses (a check).
    assert getattr(refused.value.orig, "sqlstate", None) in {"42501", "23514"}
    await insert_as_worker(worker_engine, alpha, {"org_id": alpha.id, **BUDGET})  # control
    assert len(await rows(owner_engine, alpha)) == 1


async def test_the_worker_cannot_read_change_or_delete_the_audit_log(
    worker_engine: AsyncEngine, alpha: Org
) -> None:
    for statement in (
        "SELECT count(*) FROM audit_logs",
        "UPDATE audit_logs SET outcome = 'failure'",
        "DELETE FROM audit_logs",
    ):
        with pytest.raises(DBAPIError) as refused:
            async with worker_engine.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('app.org_id', :org, true)"), {"org": str(alpha.id)}
                )
                await connection.execute(text(statement))
        assert getattr(refused.value.orig, "sqlstate", None) == "42501", statement


async def test_the_worker_cannot_write_into_another_organization(
    worker_engine: AsyncEngine, alpha_beta: tuple[Org, Org]
) -> None:
    alpha, beta = alpha_beta
    with pytest.raises(DBAPIError):
        await insert_as_worker(worker_engine, alpha, {"org_id": beta.id, **BUDGET})


async def test_the_application_role_still_writes_every_other_action(
    owner_engine: AsyncEngine, alpha: Org, database: TestDatabase
) -> None:
    """The trigger concerns the worker only: the API's own audit entries are unchanged."""
    from sqlalchemy.ext.asyncio import create_async_engine

    app = create_async_engine(database.app_url)
    try:
        async with app.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.org_id', :org, true)"), {"org": str(alpha.id)}
            )
            await connection.execute(
                text(
                    "INSERT INTO audit_logs (org_id, actor_type, action, target, outcome) "
                    "VALUES (:o, 'user', 'user.role_changed', 'user:x', 'success')"
                ),
                {"o": alpha.id},
            )
    finally:
        await app.dispose()


async def test_migration_0013_goes_down_and_up_again(
    database: TestDatabase, owner_engine: AsyncEngine
) -> None:
    async def worker_may_insert() -> bool:
        async with owner_engine.connect() as connection:
            row = await connection.execute(
                text(
                    "SELECT has_column_privilege('pharos_worker', 'public.audit_logs', "
                    "'action', 'INSERT')"
                )
            )
            return bool(row.scalar_one())

    assert await worker_may_insert() is True
    await asyncio.to_thread(command.downgrade, database.alembic, "0012")
    try:
        assert await worker_may_insert() is False
    finally:
        await asyncio.to_thread(command.upgrade, database.alembic, "head")
    assert await worker_may_insert() is True
