"""The budget events of the gateway, written into the audit log by the worker (F-39, ADR-0016).

`llm.budget_warning` when the day's spending first crosses 80%, `llm.budget_exhausted` when a call
is first refused at 100%. The worker's role may write these two actions and nothing else into the
audit log (migration 0013); the row names the organization and nothing about a person or a call.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.audit import AuditAction

BUDGET_EVENTS = frozenset(
    {AuditAction.LLM_BUDGET_WARNING.value, AuditAction.LLM_BUDGET_EXHAUSTED.value}
)


class DatabaseBudgetEvents:
    """The gateway's `on_event`, as the worker role, in a transaction of its own."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def __call__(self, name: str, org_id: uuid.UUID) -> None:
        if name not in BUDGET_EVENTS:
            raise ValueError("not a budget event")
        async with self._engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org_id)}
            )
            await connection.execute(
                text(
                    "INSERT INTO audit_logs (org_id, actor_type, action, outcome) "
                    "VALUES (:org, 'system', :action, 'success')"
                ),
                {"org": org_id, "action": name},
            )
