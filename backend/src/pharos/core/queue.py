"""Sending a task to the worker by name, from the API (ADR-0013).

The API never imports the worker's code (an import contract forbids it): it only knows a task's
name and sends identifiers to the broker.

The message goes to the queue by name, through the anonymous exchange. Celery's usual route, a
named exchange, makes kombu read the table of bindings (`_kombu.binding.celery`) and push the
message onto every queue listed there. The worker's Redis user may write that table, and the API's
Redis user may write any key, so a compromised worker could add a binding that points at a key of
the API's and make the API write into it. With the anonymous exchange the table is neither read nor
written, and the API touches exactly one key: the list `celery`.

The Celery client is blocking, so the send runs in a small thread pool of its own (not the default
one, which name look-ups also use) under a deadline of its own, with short socket timeouts and no
retry loop: if the broker does not answer, the request fails (`503`) instead of hanging.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from celery import Celery
from kombu import Exchange, Queue

from pharos.core.errors import DependencyUnavailable

logger = logging.getLogger(__name__)

QUEUE = "celery"  # the queue the worker listens to (Celery's default)
BROKER_TIMEOUT_SECONDS = 2
DEADLINE_SECONDS = 6.0
POOL_SIZE = 4


class CeleryQueue:
    def __init__(self, broker_url: str, *, deadline_seconds: float = DEADLINE_SECONDS) -> None:
        # A client of its own: it is not the default application and has no tasks.
        self._app = Celery("pharos-api", broker=broker_url, set_as_current=False)
        direct = Queue(QUEUE, Exchange("", type="direct"), routing_key=QUEUE)
        self._app.conf.update(
            task_serializer="json",
            accept_content=["json"],
            task_queues=(direct,),
            task_default_queue=QUEUE,
            task_default_exchange="",
            task_default_routing_key=QUEUE,
            broker_transport_options={
                "socket_timeout": BROKER_TIMEOUT_SECONDS,
                "socket_connect_timeout": BROKER_TIMEOUT_SECONDS,
            },
            broker_connection_timeout=BROKER_TIMEOUT_SECONDS,
            task_publish_retry=False,
        )
        self._deadline = deadline_seconds
        self._pool = ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="queue-send")

    def _send(self, name: str, args: list[str], expires: int) -> None:
        self._app.send_task(name, args=args, expires=expires, exchange="", routing_key=QUEUE)

    async def send(self, name: str, args: list[str], *, expires: int) -> None:
        """Send the task. Raises DependencyUnavailable if the broker cannot be reached in time."""
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(self._deadline):
                await loop.run_in_executor(self._pool, self._send, name, args, expires)
        except Exception as error:  # kombu, redis, socket and timeouts: all mean "not sent"
            logger.error("queue: could not send a task", extra={"error": type(error).__name__})
            raise DependencyUnavailable from None
