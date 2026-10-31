"""The model-layer page for administrators.

What the page shows comes from three places and none of them is the key: the settings the process
started with (provider, models, cap), the counters the worker keeps in Redis (what has been spent
today), and the worker's record of calls in `llm_usage`. "Test the key" is a task for the worker;
its answer is read as untrusted data (ADR-0013).

Like the sources service, this one has no network in it and imports nothing that has.
"""

import json
import logging
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

from redis.exceptions import RedisError

from pharos.core.audit import AuditAction, AuditLog
from pharos.core.clock import Clock
from pharos.core.errors import ApiError, DependencyUnavailable, ErrorCode, not_found
from pharos.core.principal import Principal
from pharos.core.unit_of_work import UnitOfWork
from pharos.llm import keytest
from pharos.llm.budget import spent_key
from pharos.llmadmin import repository
from pharos.llmadmin.schemas import (
    KeyCheckOut,
    KeyTestOut,
    KeyTestStartedOut,
    ModelsOut,
    StatusOut,
    UsageDayOut,
)

logger = logging.getLogger(__name__)

USAGE_DAYS = 7
REDIS_TIMEOUT_SECONDS = 3


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """The non-secret settings the page shows."""

    provider: str
    model_filter: str
    model_enrichment: str
    model_insight: str
    daily_cap_micro_usd: int


class TaskQueue(Protocol):
    def send(self, name: str, args: list[str], *, expires: int) -> Awaitable[None]: ...


class KeyValues(Protocol):
    def set(self, name: str, value: str, *, ex: int, nx: bool = ...) -> Awaitable[Any]: ...
    def get(self, name: str) -> Awaitable[Any]: ...
    def delete(self, *names: str) -> Awaitable[Any]: ...
    def ttl(self, name: str) -> Awaitable[Any]: ...
    def exists(self, *names: str) -> Awaitable[Any]: ...


class ModelAdminService:
    def __init__(
        self,
        *,
        audit: AuditLog,
        clock: Clock,
        settings: ModelSettings,
        redis: KeyValues | None = None,
        queue: TaskQueue | None = None,
    ) -> None:
        self._audit = audit
        self._clock = clock
        self._settings = settings
        self._redis = redis
        self._queue = queue

    async def _redis_call(self, call: Awaitable[Any]) -> Any:
        try:
            return await call
        except (RedisError, OSError, TimeoutError):
            raise DependencyUnavailable from None

    # --- the page ---

    async def status(self, uow: UnitOfWork) -> StatusOut:
        if self._redis is None:
            raise DependencyUnavailable
        usage = await repository.usage_by_day(uow.session, uow.org_id, days=USAGE_DAYS)
        await uow.release()
        today = self._clock.now().strftime("%Y%m%d")
        key = spent_key(uow.org_id, today)
        raw_spent = await self._redis_call(self._redis.get(key))
        warned = await self._redis_call(self._redis.exists(f"{key}:warned"))
        exhausted = await self._redis_call(self._redis.exists(f"{key}:exhausted"))
        last = await self._redis_call(self._redis.get(keytest.last_key(uow.org_id)))
        checked = keytest.parse_result(last, test_id=None)
        settings = self._settings
        return StatusOut(
            provider=settings.provider,  # type: ignore[arg-type]
            models=ModelsOut(
                filter=settings.model_filter,
                enrichment=settings.model_enrichment,
                insight=settings.model_insight,
            ),
            daily_cap_micro_usd=settings.daily_cap_micro_usd,
            spent_micro_usd=_whole_number(raw_spent),
            warned=bool(warned),
            exhausted=bool(exhausted),
            key_check=None
            if checked is None
            else KeyCheckOut(outcome=checked.outcome, checked_at=checked.checked_at),
            usage=[
                UsageDayOut(
                    day=u.day,
                    stage=u.stage,  # type: ignore[arg-type]
                    calls=u.calls,
                    refused=u.refused,
                    failed=u.failed,
                    tokens_in=u.tokens_in,
                    tokens_out=u.tokens_out,
                    cost_micro_usd=u.cost_micro_usd,
                )
                for u in usage
            ],
        )

    # --- "test the key" ---

    async def start_key_test(
        self, uow: UnitOfWork, actor: Principal, *, ip: str | None
    ) -> KeyTestStartedOut:
        if self._redis is None or self._queue is None:
            raise DependencyUnavailable
        cooldown = keytest.cooldown_key(uow.org_id)
        taken = await self._redis_call(
            self._redis.set(cooldown, "1", ex=keytest.COOLDOWN_SECONDS, nx=True)
        )
        if not taken:
            wait = await self._redis_call(self._redis.ttl(cooldown))
            seconds = wait if isinstance(wait, int) and wait > 0 else keytest.COOLDOWN_SECONDS
            await uow.release()
            raise ApiError(ErrorCode.RATE_LIMITED, 429, headers={"Retry-After": str(seconds)})
        test_id = uuid.uuid4()
        await self._audit.record(
            uow.session,
            org_id=uow.org_id,
            action=AuditAction.LLM_KEY_TESTED,
            actor_id=actor.user_id,
            target="llm",
            ip=ip,
            details={"provider": self._settings.provider},
        )
        await uow.commit()
        marker = keytest.marker_key(test_id)
        try:
            await self._redis_call(
                self._redis.set(
                    marker,
                    json.dumps({"org": str(uow.org_id), "by": str(actor.user_id)}),
                    ex=keytest.MARKER_TTL_SECONDS,
                )
            )
            await self._queue.send(
                keytest.TASK_NAME, [str(test_id)], expires=keytest.TASK_EXPIRES_SECONDS
            )
        except DependencyUnavailable:
            # The marker goes; the cooldown stays: the task may have reached the broker even
            # though the answer did not reach us, and a retry at once would then call twice.
            try:
                await self._redis_call(self._redis.delete(marker))
            except DependencyUnavailable:
                logger.warning("key test: could not clean up after a failed start")
            raise
        return KeyTestStartedOut(test_id=test_id, retry_after_seconds=keytest.COOLDOWN_SECONDS)

    async def key_test_result(self, uow: UnitOfWork, test_id: uuid.UUID) -> KeyTestOut:
        """The worker's answer, if there is one. It is shown only if it passes the strict reader and
        belongs to this test, and the test was started by this organization."""
        if self._redis is None:
            raise DependencyUnavailable
        await uow.release()
        raw_marker = await self._redis_call(self._redis.get(keytest.marker_key(test_id)))
        try:
            marker = json.loads(raw_marker) if raw_marker is not None else None
        except ValueError:
            marker = None
        if not isinstance(marker, dict) or marker.get("org") != str(uow.org_id):
            raise not_found()  # unknown, expired, or another organization's: the same answer
        raw = await self._redis_call(self._redis.get(keytest.result_key(test_id)))
        if raw is None:
            return KeyTestOut(status="pending", outcome=None, checked_at=None)
        result = keytest.parse_result(raw, test_id=test_id)
        if result is None:
            return KeyTestOut(status="unavailable", outcome=None, checked_at=None)
        return KeyTestOut(status="done", outcome=result.outcome, checked_at=result.checked_at)


def _whole_number(raw: Any) -> int:
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)
