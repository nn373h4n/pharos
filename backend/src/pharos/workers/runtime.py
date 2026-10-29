"""Database access for Celery tasks: one event loop and one engine per worker process.

Celery tasks are plain functions and asyncpg connections belong to the loop that opened them, so
each (forked) worker process keeps its own loop and engine and runs every task with
`runtime.run(...)`. Nothing is created at import time or in the parent process: the first task in a
process creates them, so a process that never touches the database (the heartbeat task, the
scheduler) needs no database at all, and a fork never inherits a live connection.

Tasks must not hold a database transaction while they wait for the network: fetch first, then open
a transaction, write, and close it (ADR-0012).
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.config import get_settings
from pharos.core.db import create_engine

logger = logging.getLogger(__name__)

WORKER_ROLE = "pharos_worker"

OLD_VOLUME_HINT = (
    "The worker could not use its database role. A database created before M3 has no "
    f"{WORKER_ROLE} role: recreate the volumes (docs/DEPLOYMENT.md, 'Upgrading to M3')."
)


class WorkerRoleError(RuntimeError):
    """The worker is connected as the wrong role, or as one that is too powerful."""


async def check_worker_role(engine: AsyncEngine) -> None:
    """Refuse to start unless the connection is the limited worker role.

    The worker handles content from outside. If it were given the application role (or worse, the
    owner), a bug in a connector could read people and sign-in data, so this is checked once when
    the worker starts and fails loudly.
    """
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT current_user AS name, "
                    "(r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb) "
                    "AS privileged, "
                    "EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid) AS member, "
                    # Tables, views, sequences and indexes, functions, schemas and the database.
                    "(EXISTS (SELECT 1 FROM pg_class c WHERE c.relowner = r.oid) "
                    "OR EXISTS (SELECT 1 FROM pg_proc p WHERE p.proowner = r.oid) "
                    "OR EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspowner = r.oid) "
                    "OR EXISTS (SELECT 1 FROM pg_database d WHERE d.datdba = r.oid)) "
                    "AS owns_objects "
                    "FROM pg_roles r WHERE r.rolname = current_user"
                )
            )
        ).one()
    if row.name != WORKER_ROLE:
        raise WorkerRoleError(f"the worker is connected as {row.name!r}, not {WORKER_ROLE!r}")
    if row.privileged or row.member or row.owns_objects:
        raise WorkerRoleError(
            f"{WORKER_ROLE} has special attributes, is a member of another role or owns objects"
        )


class WorkerRuntime:
    """Lazily creates, per process, the event loop and the engine that tasks share."""

    def __init__(self, database_url: Callable[[], str] | None = None) -> None:
        self._database_url = database_url or (
            lambda: get_settings().database_url.get_secret_value()
        )
        # (pid, loop, engine) of the process that created them; None before the first use.
        self._state: tuple[int, asyncio.AbstractEventLoop, AsyncEngine] | None = None

    def _ensure(self) -> tuple[asyncio.AbstractEventLoop, AsyncEngine]:
        state = self._state
        if state is None or state[0] != os.getpid():
            # First use in this process. After a fork the inherited loop and engine belong to the
            # parent: drop the references without closing anything, and start fresh.
            state = (os.getpid(), asyncio.new_event_loop(), create_engine(self._database_url()))
            self._state = state
        return state[1], state[2]

    def run[T](self, work: Callable[[AsyncEngine], Awaitable[T]]) -> T:
        """Run one task body to completion on this process's loop, with this process's engine."""
        loop, engine = self._ensure()
        task: asyncio.Future[T] = asyncio.ensure_future(work(engine), loop=loop)
        try:
            return loop.run_until_complete(task)
        except BaseException:
            # A time limit raises inside run_until_complete and leaves the task suspended. Cancel it
            # and let it finish, or the loop would resume it, with its connection and transaction,
            # in the middle of the next task.
            if not task.done():
                task.cancel()
                with suppress(BaseException):
                    loop.run_until_complete(task)
            raise

    def close(self) -> None:
        state = self._state
        if state is None or state[0] != os.getpid():
            return
        _, loop, engine = state
        self._state = None
        try:
            loop.run_until_complete(engine.dispose())
        finally:
            loop.close()


runtime = WorkerRuntime()


def verify_database_role() -> None:
    """Called once by the worker's main process before it starts consuming tasks."""
    probe = WorkerRuntime()
    try:
        probe.run(check_worker_role)
    except WorkerRoleError:
        raise
    except Exception as error:
        raise WorkerRoleError(f"{OLD_VOLUME_HINT} ({type(error).__name__})") from None
    finally:
        probe.close()
