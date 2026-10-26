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
        broker_connection_retry_on_startup=True,
        beat_schedule={
            "heartbeat": {"task": HEARTBEAT_TASK, "schedule": float(HEARTBEAT_INTERVAL_SECONDS)},
        },
    )
    app.set_default()
    return app
