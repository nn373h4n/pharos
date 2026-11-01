"""Asking for the insight of an article.

The person never says which profession or language: both come from their profile, so the cache
holds only pairs that have people and nobody can make the product pay for a pair that does not
exist. The first view of a card asks the worker (by identifiers, once per key and five minutes,
through a marker in Redis); later views read the stored row. A stored row is checked again before
it is served, so a row somebody wrote around the checks is never shown.

Like the other services, this one has no network in it and imports no provider.
"""

import contextlib
import logging
import uuid
from collections.abc import Awaitable
from typing import Any, Protocol

from redis.exceptions import RedisError

from pharos.core.errors import ApiError, DependencyUnavailable, ErrorCode, not_found
from pharos.core.principal import Principal
from pharos.core.ratelimit import RateLimiter, rate_limited
from pharos.core.unit_of_work import UnitOfWork
from pharos.insights import repository
from pharos.insights.schemas import InsightBody, InsightOut
from pharos.insights.validator import validate_insight
from pharos.llm.prompt import PROFESSION_CODE, PROMPT_VERSIONS
from pharos.llm.types import Task
from pharos.profile.service import ProfileService

logger = logging.getLogger(__name__)

TASK_NAME = "pharos.llm.insight"
MARKER_SECONDS = 300
TASK_EXPIRES_SECONDS = 300
ASKS_PER_HOUR = 120


class TaskQueue(Protocol):
    def send(self, name: str, args: list[str], *, expires: int) -> Awaitable[None]: ...


class KeyValues(Protocol):
    def set(self, name: str, value: str, *, ex: int, nx: bool = ...) -> Awaitable[Any]: ...
    def delete(self, *names: str) -> Awaitable[Any]: ...


def marker_key(
    org: uuid.UUID, item: uuid.UUID, profession: str, language: str, version: str
) -> str:
    return f"pharos:api:insight:{org}:{item}:{profession}:{language}:{version}"


class InsightService:
    def __init__(
        self,
        *,
        profile: ProfileService,
        provider: str,
        redis: KeyValues | None = None,
        queue: TaskQueue | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._profile = profile
        self._provider = provider
        self._redis = redis
        self._queue = queue
        self._limiter = limiter

    async def get(
        self,
        uow: UnitOfWork,
        actor: Principal,
        item_id: uuid.UUID,
        accept_language: str | None,
    ) -> InsightOut:
        profile = await self._profile.get(uow, actor.user_id, accept_language)
        professions = await self._profile.professions(uow)
        code = next((p.code for p in professions if p.id == profile.profession_id), None)
        if code is None:
            raise ApiError(ErrorCode.ONBOARDING_INCOMPLETE, 409)
        if not PROFESSION_CODE.fullmatch(code):
            # A code the prompt cannot carry: there will never be an insight for it.
            return InsightOut(status="unavailable", reason="invalid", insight=None)
        language = profile.effective_locale
        if not await repository.item_is_readable(uow.session, uow.org_id, item_id):
            raise not_found()  # also for an article that is held, hidden, personal or not ours
        version = PROMPT_VERSIONS[Task.INSIGHT]
        row = await repository.get_insight(
            uow.session, uow.org_id, item_id, code, language, version
        )
        await uow.release()
        if row is not None:
            return self._from_row(row, code, language)
        if self._provider == "off":
            return InsightOut(status="unavailable", reason="off", insight=None)
        await self._ask(uow, actor, item_id, code, language, version)
        return InsightOut(status="pending", reason=None, insight=None)

    def _from_row(self, row: repository.InsightRow, code: str, language: str) -> InsightOut:
        if row.status != "ok":
            return InsightOut(status="unavailable", reason="rejected", insight=None)
        summary, steps, prompt, note = (
            row.summary,
            row.how_to_apply,
            row.try_prompt,
            row.safety_note,
        )
        if (
            summary is None
            or steps is None
            or prompt is None
            or note is None
            or validate_insight(summary, steps, prompt, note) is not None
        ):
            # Written around the checks (or by an older rule): never shown.
            logger.warning("a stored insight failed the checks when read")
            return InsightOut(status="unavailable", reason="invalid", insight=None)
        return InsightOut(
            status="ready",
            reason=None,
            insight=InsightBody(
                profession=code,
                language=language,
                summary=summary,
                how_to_apply=steps,
                try_prompt=prompt,
                safety_note=note,
            ),
        )

    async def _ask(
        self,
        uow: UnitOfWork,
        actor: Principal,
        item_id: uuid.UUID,
        code: str,
        language: str,
        version: str,
    ) -> None:
        if self._redis is None or self._queue is None:
            raise DependencyUnavailable
        marker = marker_key(uow.org_id, item_id, code, language, version)
        try:
            if not await self._redis.set(marker, "1", ex=MARKER_SECONDS, nx=True):
                return  # somebody asked a moment ago; the worker is on it
        except (RedisError, OSError, TimeoutError):
            raise DependencyUnavailable from None
        try:
            if self._limiter is not None:
                decision = await self._limiter.hit(
                    f"insights:{uow.org_id}:{actor.user_id}",
                    limit=ASKS_PER_HOUR,
                    window_seconds=3600,
                )
                if not decision.allowed:
                    raise rate_limited(decision.retry_after)
            await self._queue.send(
                TASK_NAME, [str(item_id), code, language], expires=TASK_EXPIRES_SECONDS
            )
        except BaseException as error:
            # No task was sent, so nobody may be left waiting on this key for five minutes.
            with contextlib.suppress(RedisError, OSError, TimeoutError):
                await self._redis.delete(marker)
            if isinstance(error, (RedisError, OSError, TimeoutError)):
                raise DependencyUnavailable from None
            raise
