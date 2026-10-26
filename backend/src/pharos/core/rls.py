"""Row-level security helpers (ADR-0005).

Every transaction that touches tenant data sets `app.org_id` from the authenticated session.
Policies compare against `pharos_current_org()`, which maps an unset or emptied setting to NULL,
so a missing organization matches no rows instead of raising an error.
"""

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pharos.core.logging import org_id_var

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


async def set_current_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Scope the current transaction to one organization (equivalent to SET LOCAL)."""
    await session.execute(
        text("SELECT set_config('app.org_id', :org_id, true)"), {"org_id": str(org_id)}
    )


@asynccontextmanager
async def tenant_session(
    factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> AsyncIterator[AsyncSession]:
    """Open a session and a transaction already scoped to `org_id`."""
    async with factory() as session, session.begin():
        await set_current_org(session, org_id)
        token = org_id_var.set(str(org_id))
        try:
            yield session
        finally:
            org_id_var.reset(token)


def _identifier(name: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def tenant_policy_sql(table: str, org_column: str = "org_id") -> list[str]:
    """Statements that put a tenant-plane table under RLS. Use them in Alembic migrations."""
    table, column = _identifier(table), _identifier(org_column)
    predicate = f"{column} = pharos_current_org()"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING ({predicate}) WITH CHECK ({predicate})",
    ]


def content_policy_sql(table: str, public_writer_role: str | None = None) -> list[str]:
    """Statements for content-plane tables with `scope` and `owner_org_id` columns.

    Reading: public rows (`scope = 'public'`, no owner) are visible to everyone, organization-owned
    rows only to their owner. Writing: the application role may only create, change and delete rows
    owned by the current organization, so no tenant can touch the public rows every tenant shares.
    Public rows are written by `public_writer_role` (the ingestion worker) or by the table owner.
    The CHECK constraint ties `scope` to `owner_org_id`.
    """
    table = _identifier(table)
    owned = "owner_org_id = pharos_current_org()"
    statements = [
        f"ALTER TABLE {table} ADD CONSTRAINT {table}_scope_owner_check CHECK ("
        "(scope = 'public' AND owner_org_id IS NULL) "
        "OR (scope = 'org' AND owner_org_id IS NOT NULL))",
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_content_select ON {table} FOR SELECT "
        f"USING (scope = 'public' OR {owned})",
        f"CREATE POLICY {table}_content_insert ON {table} FOR INSERT WITH CHECK ({owned})",
        f"CREATE POLICY {table}_content_update ON {table} FOR UPDATE "
        f"USING ({owned}) WITH CHECK ({owned})",
        f"CREATE POLICY {table}_content_delete ON {table} FOR DELETE USING ({owned})",
    ]
    if public_writer_role is not None:
        role = _identifier(public_writer_role)
        statements.append(
            f"CREATE POLICY {table}_public_writer ON {table} FOR ALL TO {role} "
            "USING (scope = 'public') WITH CHECK (scope = 'public' AND owner_org_id IS NULL)"
        )
    return statements
