"""Async database engine and session factory."""

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(url: str) -> AsyncEngine:
    # hide_parameters keeps bound values (emails, ids, future encrypted keys) out of error text.
    engine = create_async_engine(url, pool_pre_ping=True, hide_parameters=True)

    @event.listens_for(engine.sync_engine, "checkin")
    def _forget_organization(dbapi_connection: Any, _record: Any) -> None:
        # A session-level `app.org_id` must never survive on a pooled connection: the next request
        # would inherit another organization. Transaction-level settings are gone after commit.
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("RESET app.org_id")
        finally:
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
