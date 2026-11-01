"""Celery application factory.

Importing this module reads no environment variables. The Celery CLI needs a module-level
application, which `pharos.workers.entrypoint` provides.
"""

import logging
from typing import Any

from celery import Celery, signals

from pharos.core.config import Settings, get_settings
from pharos.core.heartbeat import HEARTBEAT_INTERVAL_SECONDS
from pharos.core.logging import configure_logging
from pharos.workers import llm_tasks
from pharos.workers.ingest_tasks import CLEANUP_TASK, DISPATCH_TASK
from pharos.workers.runtime import WorkerRoleError, runtime, verify_database_role
from pharos.workers.tasks import HEARTBEAT_TASK, write_heartbeat

logger = logging.getLogger(__name__)


# Connecting to setup_logging stops Celery from installing its own handlers, so worker logs get the
# same JSON format and secret masking as the API.
@signals.setup_logging.connect
def _use_pharos_logging(**_: Any) -> None:
    configure_logging(get_settings().log_level)


# Without this the worker would look down until the first scheduled beat, a minute after start.
@signals.worker_ready.connect
def _record_first_heartbeat(**_: Any) -> None:
    write_heartbeat()
    logger.info("worker ready")


# The main process checks once, before it takes any task, that it is connected as the limited worker
# role. Only the real worker process does: the API and the tests have no such role to check.
@signals.worker_init.connect
def _check_database_role(**_: Any) -> None:
    if get_settings().process != "worker":
        return
    try:
        verify_database_role()
    except WorkerRoleError as error:
        logger.critical("worker refuses to start: %s", error)
        raise SystemExit(1) from None


@signals.worker_process_shutdown.connect
def _close_runtime(**_: Any) -> None:
    runtime.close()


# A task that runs longer than this is a bug or an attack (a slow host, a feed bomb); it is stopped
# so it cannot hold a worker slot forever. The soft limit lets the task clean up first.
TASK_SOFT_TIME_LIMIT_SECONDS = 120
TASK_TIME_LIMIT_SECONDS = 150
# Child processes are replaced regularly, and when they grow past this size (KiB), so a parser that
# leaks or a hostile input that bloats memory cannot keep the memory.
WORKER_MAX_TASKS_PER_CHILD = 200
WORKER_MAX_MEMORY_PER_CHILD_KIB = 256_000


def create_celery(settings: Settings | None = None) -> Celery:
    settings = settings or get_settings()
    app = Celery("pharos", broker=settings.redis_url.get_secret_value())
    app.conf.update(
        # Stage results are written to the database, not to the result backend.
        task_ignore_result=True,
        task_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # Run a task again if the worker dies mid-way; this is why tasks must be idempotent.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_soft_time_limit=TASK_SOFT_TIME_LIMIT_SECONDS,
        task_time_limit=TASK_TIME_LIMIT_SECONDS,
        worker_max_tasks_per_child=WORKER_MAX_TASKS_PER_CHILD,
        worker_max_memory_per_child=WORKER_MAX_MEMORY_PER_CHILD_KIB,
        broker_connection_retry_on_startup=True,
        beat_schedule={
            "heartbeat": {"task": HEARTBEAT_TASK, "schedule": float(HEARTBEAT_INTERVAL_SECONDS)},
            # Every minute: lease the sources that are due and hand each to a worker.
            "ingest-dispatch": {"task": DISPATCH_TASK, "schedule": 60.0},
            # Retention and abandoned runs; safe to run as often as it is.
            "ingest-cleanup": {"task": CLEANUP_TASK, "schedule": 6 * 3600.0},
            # Every minute: the model reads a few articles nobody has read yet (a no-op while the
            # model layer is off).
            "llm-enrich-sweep": {"task": llm_tasks.SWEEP_TASK, "schedule": 60.0},
        },
    )
    app.set_default()
    return app
