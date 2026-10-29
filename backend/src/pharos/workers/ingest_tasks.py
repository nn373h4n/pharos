"""Celery tasks of the ingestion pipeline. Thin on purpose: the logic is in `pharos.ingest`.

The fetch task carries two identifiers and nothing else (ADR-0012); the address to fetch, the
configuration and the validators are read from the database inside the task, after the lease has
been checked. Every task is safe to run twice.
"""

import logging
import uuid
from datetime import UTC, datetime

from celery import shared_task
from redis import asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncEngine

from pharos.core.config import get_settings
from pharos.ingest import repository
from pharos.ingest.connectors import build_connectors
from pharos.ingest.dispatcher import LEASE_SECONDS, dispatch
from pharos.ingest.fetcher import MIN_LEASE_LEFT, fetch_source
from pharos.ingest.hostgate import HostGate
from pharos.net.safe_http import SafeHttpClient
from pharos.workers.runtime import runtime

logger = logging.getLogger(__name__)

DISPATCH_TASK = "pharos.ingest.dispatch"
FETCH_TASK = "pharos.ingest.fetch"
CLEANUP_TASK = "pharos.ingest.cleanup"


def _now() -> datetime:
    return datetime.now(UTC)


def enqueue_fetch(source_id: uuid.UUID, lease_token: uuid.UUID) -> None:
    # A task that waits so long that too little lease is left would be refused anyway.
    fetch_task.apply_async(
        args=[str(source_id), str(lease_token)],
        expires=LEASE_SECONDS - int(MIN_LEASE_LEFT.total_seconds()),
    )


@shared_task(name=DISPATCH_TASK, ignore_result=True)
def dispatch_task() -> None:
    slug = get_settings().org_slug

    async def work(engine: AsyncEngine) -> None:
        report = await dispatch(engine, slug=slug, now=_now(), enqueue=enqueue_fetch)
        if report.leased:
            logger.info(
                "dispatch",
                extra={"leased": report.leased, "enqueued": report.enqueued},
            )

    runtime.run(work)


@shared_task(name=FETCH_TASK, ignore_result=True)
def fetch_task(source_id: str, lease_token: str) -> None:
    try:
        source, token = uuid.UUID(source_id), uuid.UUID(lease_token)
    except (ValueError, TypeError, AttributeError):
        logger.warning("fetch: the task arguments are not identifiers; dropped")
        return
    settings = get_settings()

    async def work(engine: AsyncEngine) -> None:
        http = SafeHttpClient.from_settings(settings)
        github_token = settings.github_token.get_secret_value() if settings.github_token else None
        connectors = build_connectors(github_token)
        redis = aioredis.from_url(settings.redis_url.get_secret_value())
        try:
            report = await fetch_source(
                engine,
                slug=settings.org_slug,
                source_id=source,
                lease_token=token,
                clock=_now,
                connectors=connectors,
                http=http,
                gate=HostGate(redis),
            )
        finally:
            await http.aclose()
            await redis.aclose()
        logger.info("fetch", extra={"result": report.result, "items_new": report.items_new})

    runtime.run(work)


@shared_task(name=CLEANUP_TASK, ignore_result=True)
def cleanup_task() -> None:
    slug = get_settings().org_slug

    async def work(engine: AsyncEngine) -> None:
        async with engine.begin() as connection:
            org = await repository.resolve_org(connection, slug)
            if org is None:
                return
            await repository.enter_org(connection, org)
            counts = await repository.cleanup(connection, _now(), repository.Retention())
        logger.info("cleanup", extra=counts)

    runtime.run(work)
