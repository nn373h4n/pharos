"""The per-process runtime: lazy, one loop and one engine, fork-safe, closed on shutdown."""

import asyncio
import os
import signal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.config import Settings
from pharos.workers import app as worker_app
from pharos.workers import runtime as runtime_module
from pharos.workers.runtime import WorkerRoleError, WorkerRuntime

URL = "postgresql+asyncpg://pharos_worker:unit-test-only@127.0.0.1:1/none"


def make() -> WorkerRuntime:
    return WorkerRuntime(lambda: URL)


def test_nothing_is_created_before_the_first_task() -> None:
    calls: list[int] = []
    runtime = WorkerRuntime(lambda: calls.append(1) or URL)
    assert calls == []
    runtime.close()  # closing an unused runtime is harmless


def test_every_task_in_a_process_shares_one_loop_and_one_engine() -> None:
    runtime = make()
    seen: list[tuple[int, int]] = []

    async def body(engine: AsyncEngine) -> str:
        seen.append((id(asyncio.get_running_loop()), id(engine)))
        return "done"

    try:
        assert [runtime.run(body) for _ in range(3)] == ["done"] * 3
        assert len(set(seen)) == 1
    finally:
        runtime.close()


def test_a_forked_process_starts_with_its_own_loop_and_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make()
    seen: list[tuple[int, int]] = []

    async def body(engine: AsyncEngine) -> None:
        seen.append((id(asyncio.get_running_loop()), id(engine)))

    runtime.run(body)
    parent_state = runtime._state
    assert parent_state is not None
    monkeypatch.setattr(os, "getpid", lambda: parent_state[0] + 1)  # "we are the child now"
    runtime.run(body)
    child_state = runtime._state
    assert child_state is not None
    assert seen[0] != seen[1]
    # The child never closes what it inherited (the parent still owns it).
    assert not parent_state[1].is_closed()
    monkeypatch.undo()
    for _, loop, engine in (parent_state, child_state):
        loop.run_until_complete(engine.dispose())
        loop.close()


def test_close_disposes_the_engine_and_the_next_task_starts_fresh() -> None:
    runtime = make()
    engines: list[AsyncEngine] = []

    async def body(engine: AsyncEngine) -> None:
        engines.append(engine)

    runtime.run(body)
    loop = runtime._state[1]  # type: ignore[index]
    runtime.close()
    assert loop.is_closed()
    assert runtime._state is None
    runtime.run(body)
    assert engines[0] is not engines[1]
    runtime.close()


class _Limit(BaseException):
    """Stands in for Celery's SoftTimeLimitExceeded, which is raised by a signal handler."""


def test_a_task_stopped_by_a_time_limit_is_cancelled_and_never_resumes() -> None:
    runtime = make()
    events: list[str] = []

    async def stalls(engine: AsyncEngine) -> None:
        try:
            events.append("started")
            await asyncio.sleep(30)
            events.append("finished")  # must never happen
        finally:
            events.append("cleaned up")  # the connection and transaction would be released here

    async def next_task(engine: AsyncEngine) -> str:
        await asyncio.sleep(0.2)  # long enough for a resumed zombie to run
        events.append("next")
        return "ok"

    def fire(*_: object) -> None:
        raise _Limit

    previous = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, 0.2)
    try:
        with pytest.raises(_Limit):
            runtime.run(stalls)
        assert events == ["started", "cleaned up"]  # cancelled, and its cleanup ran
        assert runtime.run(next_task) == "ok"
        assert events == ["started", "cleaned up", "next"]
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        runtime.close()


def test_a_failing_task_propagates_and_leaves_the_runtime_usable() -> None:
    runtime = make()

    async def broken(engine: AsyncEngine) -> None:
        raise ValueError("boom")

    async def fine(engine: AsyncEngine) -> str:
        return "ok"

    try:
        with pytest.raises(ValueError, match="boom"):
            runtime.run(broken)
        assert runtime.run(fine) == "ok"
    finally:
        runtime.close()


# --- start-up check and limits ------------------------------------------------------------------


def _settings(process: str) -> Settings:
    return Settings(environment="test", process=process)  # type: ignore[arg-type]


def test_the_api_process_does_not_run_the_worker_role_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_app, "get_settings", lambda: _settings("api"))
    monkeypatch.setattr(worker_app, "verify_database_role", lambda: pytest.fail("must not run"))
    worker_app._check_database_role()


def test_a_worker_that_is_connected_as_the_wrong_role_does_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse() -> None:
        raise WorkerRoleError("the worker is connected as 'pharos_app', not 'pharos_worker'")

    monkeypatch.setattr(worker_app, "get_settings", lambda: _settings("worker"))
    monkeypatch.setattr(worker_app, "verify_database_role", refuse)
    with pytest.raises(SystemExit) as stopped:
        worker_app._check_database_role()
    assert stopped.value.code == 1


def test_a_worker_that_passes_the_check_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[bool] = []
    monkeypatch.setattr(worker_app, "get_settings", lambda: _settings("worker"))
    monkeypatch.setattr(worker_app, "verify_database_role", lambda: ran.append(True))
    worker_app._check_database_role()
    assert ran == [True]


def test_a_missing_role_is_reported_with_the_upgrade_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    class Down(WorkerRuntime):
        def run(self, work):  # type: ignore[no-untyped-def]
            raise ConnectionRefusedError("password authentication failed for user pharos_worker")

    monkeypatch.setattr(runtime_module, "WorkerRuntime", Down)
    with pytest.raises(WorkerRoleError, match="recreate the volumes") as raised:
        runtime_module.verify_database_role()
    assert "pharos_worker:" not in str(raised.value)  # no connection details in the message


def test_tasks_are_time_limited_and_children_are_recycled() -> None:
    conf = worker_app.create_celery(_settings("worker")).conf
    assert 0 < conf.task_soft_time_limit < conf.task_time_limit <= 300
    assert 0 < conf.worker_max_tasks_per_child <= 1000
    assert conf.worker_max_memory_per_child > 0
    assert conf.task_acks_late is True
