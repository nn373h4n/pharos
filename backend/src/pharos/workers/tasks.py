"""Celery tasks. Every task must be idempotent and safe to retry."""

import logging
import time

from celery import shared_task
from redis import Redis

from pharos.core.config import get_settings
from pharos.core.heartbeat import record_heartbeat

logger = logging.getLogger(__name__)

HEARTBEAT_TASK = "pharos.heartbeat"


def write_heartbeat() -> None:
    """Store the current time; calling it twice is harmless."""
    client = Redis.from_url(get_settings().redis_url.get_secret_value())
    try:
        record_heartbeat(client, time.time())
    finally:
        client.close()


@shared_task(name=HEARTBEAT_TASK, ignore_result=True)
def heartbeat() -> None:
    write_heartbeat()
    logger.debug("heartbeat recorded")
