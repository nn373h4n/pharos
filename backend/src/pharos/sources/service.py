"""The organization's sources: what administrators and editors may see and change (M3-10a).

* Reading is for administrators and editors; creating, changing, enabling and deleting is for
  administrators only, because a source makes the server contact a third party.
* A configuration is checked by `pharos.ingest.sourceconfig` before it is stored, and again by the
  connector at every fetch. An address that can only be an internal machine is refused here with a
  clear code and an audit entry; `SafeHttpClient` still decides when a connection is opened.
* The service has no network in it and imports nothing that has (an import-linter contract keeps it
  so): it can validate a configuration, never fetch one.
* A refusal that matters (an internal address, a duplicate, the cap) leaves a failure row in the
  audit log, written on its own because this transaction is rolled back. Audit entries name the
  source and what kind of change it was; they never contain the configuration, which may hold
  addresses or search words.
* Two administrators changing one source queue up on its row lock; the second sees what the first
  left behind.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Collection
from typing import Any, NoReturn, Protocol

from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError

from pharos.core.audit import ActorType, AuditAction, AuditLog, Outcome
from pharos.core.errors import ApiError, DependencyUnavailable, ErrorCode, not_found
from pharos.core.principal import Principal
from pharos.core.unit_of_work import UnitOfWork
from pharos.ingest import testfetch
from pharos.ingest.sourceconfig import ConfigError, validate
from pharos.ingest.text import clean_text
from pharos.sources import repository
from pharos.sources.schemas import (
    SourceIn,
    SourcePatch,
    TestFetchOut,
    TestFetchStartedOut,
    TestSampleOut,
)

logger = logging.getLogger(__name__)

MAX_ORG_SOURCES = 200
NAME_LIMIT = 200
REDIS_TIMEOUT_SECONDS = 3


class TaskQueue(Protocol):
    def send(self, name: str, args: list[str], *, expires: int) -> Awaitable[None]: ...


class KeyValues(Protocol):
    """The few Redis commands the test fetch needs (redis.asyncio.Redis has them all)."""

    def set(self, name: str, value: str, *, ex: int, nx: bool = ...) -> Awaitable[Any]: ...
    def get(self, name: str) -> Awaitable[Any]: ...
    def delete(self, *names: str) -> Awaitable[Any]: ...
    def ttl(self, name: str) -> Awaitable[Any]: ...
    def incr(self, name: str) -> Awaitable[Any]: ...


def _invalid(field: str, kind: str) -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_FAILED, 422, errors=[{"loc": ["body", field], "type": kind}]
    )


def clean_name(name: str) -> str:
    """The name as plain text. A name that changes when cleaned (markup, control or invisible
    characters) is refused, not repaired."""
    stripped = name.strip()
    if not stripped or clean_text(stripped, NAME_LIMIT) != stripped:
        raise _invalid("name", "invalid_text")
    return stripped


class SourceService:
    def __init__(
        self,
        *,
        audit: AuditLog,
        blocked_hosts: Collection[str],
        redis: KeyValues | None = None,
        queue: TaskQueue | None = None,
    ) -> None:
        self._audit = audit
        self._blocked_hosts = tuple(blocked_hosts)
        self._redis = redis
        self._queue = queue

    # --- reading ---------------------------------------------------------------------------------

    async def list_sources(
        self,
        uow: UnitOfWork,
        *,
        limit: int,
        offset: int,
        source_type: str | None,
        enabled: bool | None,
    ) -> tuple[list[repository.SourceView], int]:
        return await repository.list_sources(
            uow.session,
            uow.org_id,
            limit=limit,
            offset=offset,
            source_type=source_type,
            enabled=enabled,
        )

    async def get(self, uow: UnitOfWork, source_id: uuid.UUID) -> repository.SourceView:
        view = await repository.get_source(uow.session, uow.org_id, source_id)
        if view is None:
            raise not_found()
        return view

    async def runs(self, uow: UnitOfWork, source_id: uuid.UUID, *, limit: int) -> list[Any]:
        await self.get(uow, source_id)  # a missing source, or another organization's, is a 404
        return await repository.recent_runs(uow.session, uow.org_id, source_id, limit=limit)

    # --- writing ---------------------------------------------------------------------------------

    async def _refuse(
        self,
        uow: UnitOfWork,
        actor: Principal,
        action: AuditAction,
        target: str | None,
        ip: str | None,
        reason: str,
        error: ApiError,
        **details: Any,
    ) -> NoReturn:
        """Refuse, and leave a trace in a transaction of its own (this one is rolled back).

        The request's own transaction ends first: it must not hold a connection and a row lock
        while the trace asks the pool for a second connection."""
        await uow.release()
        await self._audit.record_independently(
            org_id=uow.org_id,
            action=action,
            outcome=Outcome.FAILURE,
            actor_type=ActorType.USER,
            actor_id=actor.user_id,
            target=target,
            ip=ip,
            details={"reason": reason, **details},
        )
        raise error

    async def _validated(
        self,
        uow: UnitOfWork,
        actor: Principal,
        action: AuditAction,
        target: str | None,
        ip: str | None,
        source_type: str,
        config: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        try:
            checked = validate(source_type, config, blocked_hosts=self._blocked_hosts)
        except ConfigError as error:
            if error.code == "url_not_allowed":  # worth a trace: somebody pointed us inwards
                await self._refuse(
                    uow,
                    actor,
                    action,
                    target,
                    ip,
                    error.code,
                    _invalid("config", error.code),
                    type=source_type,
                )
            raise _invalid("config", error.code) from None
        return checked.config, checked.dedupe_key

    async def create(
        self, uow: UnitOfWork, actor: Principal, body: SourceIn, *, ip: str | None
    ) -> repository.SourceView:
        action = AuditAction.SOURCE_CREATED
        name = clean_name(body.name)
        config, key = await self._validated(uow, actor, action, None, ip, body.type, body.config)
        await repository.lock_org_sources(uow.session, uow.org_id)
        if await repository.count_sources(uow.session, uow.org_id) >= MAX_ORG_SOURCES:
            await self._refuse(
                uow,
                actor,
                action,
                None,
                ip,
                "limit",
                ApiError(ErrorCode.SOURCE_LIMIT, 409),
                type=body.type,
            )
        await repository.act_as(uow.session, actor.user_id)
        created = await repository.insert_source(
            uow.session,
            uow.org_id,
            source_type=body.type,
            name=name,
            config=config,
            dedupe_key=key,
            enabled=body.enabled,
            interval_seconds=body.interval_seconds,
            created_by=actor.user_id,
        )
        if created is None:
            await self._refuse(
                uow,
                actor,
                action,
                None,
                ip,
                "duplicate",
                ApiError(ErrorCode.SOURCE_CONFLICT, 409),
                type=body.type,
            )
        await self._audit.record(
            uow.session,
            org_id=uow.org_id,
            action=action,
            actor_id=actor.user_id,
            target=f"source:{created.id}",
            ip=ip,
            details={
                "type": body.type,
                "interval_seconds": body.interval_seconds,
                "enabled": body.enabled,
            },
        )
        await uow.commit()
        return created

    async def update(
        self,
        uow: UnitOfWork,
        actor: Principal,
        source_id: uuid.UUID,
        body: SourcePatch,
        *,
        ip: str | None,
    ) -> repository.SourceView:
        current = await repository.get_source(uow.session, uow.org_id, source_id, lock=True)
        if current is None:
            raise not_found()
        target = f"source:{source_id}"
        given = body.model_fields_set

        name = clean_name(body.name) if "name" in given and body.name is not None else current.name
        interval = body.interval_seconds if "interval_seconds" in given else None
        interval = current.interval_seconds if interval is None else interval
        enabled = (
            body.enabled if "enabled" in given and body.enabled is not None else current.enabled
        )
        config, key = current.config, current.dedupe_key
        if "config" in given and body.config is not None:
            config, key = await self._validated(
                uow, actor, AuditAction.SOURCE_UPDATED, target, ip, current.type, body.config
            )
        elif enabled and not current.enabled:
            # Switching a source on is when it starts to cost something: look at its address again,
            # against what is known now (the address of this installation may have changed).
            await self._validated(
                uow, actor, AuditAction.SOURCE_UPDATED, target, ip, current.type, current.config
            )
        if key != current.dedupe_key and await repository.key_taken(
            uow.session, uow.org_id, key, except_id=source_id
        ):
            await self._refuse(
                uow,
                actor,
                AuditAction.SOURCE_UPDATED,
                target,
                ip,
                "duplicate",
                ApiError(ErrorCode.SOURCE_CONFLICT, 409),
                type=current.type,
            )

        changed = [
            field
            for field, before, after in (
                ("name", current.name, name),
                ("config", current.config, config),
                ("interval_seconds", current.interval_seconds, interval),
            )
            if before != after
        ]
        flipped = enabled != current.enabled
        if not changed and not flipped:
            return current  # nothing to do, nothing to record

        try:
            updated = await repository.update_source(
                uow.session,
                uow.org_id,
                source_id,
                name=name,
                config=config,
                dedupe_key=key,
                enabled=enabled,
                interval_seconds=interval,
            )
        except IntegrityError:  # the same source was created in the meantime
            await self._refuse(
                uow,
                actor,
                AuditAction.SOURCE_UPDATED,
                target,
                ip,
                "duplicate",
                ApiError(ErrorCode.SOURCE_CONFLICT, 409),
                type=current.type,
            )
        if changed:
            await self._audit.record(
                uow.session,
                org_id=uow.org_id,
                action=AuditAction.SOURCE_UPDATED,
                actor_id=actor.user_id,
                target=target,
                ip=ip,
                details={"type": current.type, "fields": ",".join(changed)},
            )
        if flipped:
            await self._audit.record(
                uow.session,
                org_id=uow.org_id,
                action=AuditAction.SOURCE_ENABLED if enabled else AuditAction.SOURCE_DISABLED,
                actor_id=actor.user_id,
                target=target,
                ip=ip,
                details={"type": current.type},
            )
        await uow.commit()
        return updated

    async def delete(
        self, uow: UnitOfWork, actor: Principal, source_id: uuid.UUID, *, ip: str | None
    ) -> None:
        current = await repository.get_source(uow.session, uow.org_id, source_id, lock=True)
        if current is None or not await repository.delete_source(
            uow.session, uow.org_id, source_id
        ):
            raise not_found()
        await self._audit.record(
            uow.session,
            org_id=uow.org_id,
            action=AuditAction.SOURCE_DELETED,
            actor_id=actor.user_id,
            target=f"source:{source_id}",
            ip=ip,
            details={"type": current.type, "enabled": current.enabled},
        )
        await uow.commit()

    # --- the trial fetch (ADR-0013) -----------------------------------------------------------

    async def _redis_call(self, call: Awaitable[Any]) -> Any:
        """One Redis command, with a deadline; a failure means the dependency is unavailable."""
        try:
            async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
                return await call
        except (RedisError, TimeoutError, OSError):
            raise DependencyUnavailable from None

    async def start_test(
        self, uow: UnitOfWork, actor: Principal, source_id: uuid.UUID, *, ip: str | None
    ) -> TestFetchStartedOut:
        """Ask the worker for one trial fetch of this source. Returns at once; the answer is read
        later with `test_result`."""
        if self._redis is None or self._queue is None:
            raise DependencyUnavailable
        action = AuditAction.SOURCE_TEST_FETCH
        current = await self.get(uow, source_id)
        target = f"source:{source_id}"
        # The stored address is looked at again: the address of this installation may have changed
        # since it was saved, and a trial fetch is an outbound request caused by a person.
        await self._validated(uow, actor, action, target, ip, current.type, current.config)
        cooldown = testfetch.cooldown_key(uow.org_id, source_id)
        taken = await self._redis_call(
            self._redis.set(cooldown, "1", ex=testfetch.COOLDOWN_SECONDS, nx=True)
        )
        if not taken:
            wait = await self._redis_call(self._redis.ttl(cooldown))
            seconds = wait if isinstance(wait, int) and wait > 0 else testfetch.COOLDOWN_SECONDS
            await uow.release()
            raise ApiError(ErrorCode.RATE_LIMITED, 429, headers={"Retry-After": str(seconds)})
        # The organization as a whole may not start more than a few trials a minute, whatever the
        # number of sources and administrators: a trial is an outbound request caused by a person.
        rate = testfetch.rate_key(uow.org_id)
        await self._redis_call(
            self._redis.set(rate, "0", ex=testfetch.RATE_WINDOW_SECONDS, nx=True)
        )
        if await self._redis_call(self._redis.incr(rate)) > testfetch.ORG_TESTS_PER_MINUTE:
            wait = await self._redis_call(self._redis.ttl(rate))
            await self._redis_call(self._redis.delete(cooldown))  # this one did not start
            await uow.release()
            raise ApiError(
                ErrorCode.RATE_LIMITED,
                429,
                headers={"Retry-After": str(wait if isinstance(wait, int) and wait > 0 else 1)},
            )
        test_id = uuid.uuid4()
        await self._audit.record(
            uow.session,
            org_id=uow.org_id,
            action=action,
            actor_id=actor.user_id,
            target=target,
            ip=ip,
            details={"type": current.type},
        )
        await uow.commit()
        marker = testfetch.marker_key(test_id)
        try:
            await self._redis_call(
                self._redis.set(
                    marker,
                    json.dumps(
                        {"org": str(uow.org_id), "source": str(source_id), "by": str(actor.user_id)}
                    ),
                    ex=testfetch.MARKER_TTL_SECONDS,
                )
            )
            await self._queue.send(
                testfetch.TASK_NAME,
                [str(source_id), str(test_id)],
                expires=testfetch.TASK_EXPIRES_SECONDS,
            )
        except DependencyUnavailable:
            # The marker goes. The cooldown stays: the task may have reached the broker even though
            # the answer did not reach us, and a retry at once would then fetch the source twice.
            try:
                await self._redis_call(self._redis.delete(marker))
            except DependencyUnavailable:
                logger.warning("test fetch: could not clean up after a failed start")
            await self._audit.record_independently(
                org_id=uow.org_id,
                action=action,
                outcome=Outcome.FAILURE,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                target=target,
                ip=ip,
                details={"reason": "unavailable", "type": current.type},
            )
            raise
        return TestFetchStartedOut(test_id=test_id, retry_after_seconds=testfetch.COOLDOWN_SECONDS)

    async def test_result(
        self, uow: UnitOfWork, source_id: uuid.UUID, test_id: uuid.UUID
    ) -> TestFetchOut:
        """The answer of a trial fetch, if there is one. What the worker left is untrusted: it is
        shown only if it passes the protocol's strict reader and belongs to this test."""
        if self._redis is None:
            raise DependencyUnavailable
        await self.get(uow, source_id)  # a missing source, or another organization's, is a 404
        await uow.release()
        raw_marker = await self._redis_call(self._redis.get(testfetch.marker_key(test_id)))
        try:
            marker = json.loads(raw_marker) if raw_marker is not None else None
        except ValueError:
            marker = None
        if (
            not isinstance(marker, dict)
            or marker.get("org") != str(uow.org_id)
            or marker.get("source") != str(source_id)
        ):
            raise not_found()  # not asked for here, or already forgotten
        raw = await self._redis_call(self._redis.get(testfetch.result_key(test_id)))
        if raw is None:
            return TestFetchOut(status="pending")
        result = testfetch.parse_result(raw, test_id=test_id, source_id=source_id)
        if result is None:
            logger.warning("test fetch: the worker left something that is not an answer")
            return TestFetchOut(status="unavailable")
        return TestFetchOut(
            status="done",
            outcome=result.outcome,
            error_code=result.error_code,
            http_status=result.http_status,
            entries_seen=result.entries_seen,
            entries_skipped=result.entries_skipped,
            bytes_fetched=result.bytes_fetched,
            sample=[
                TestSampleOut(title=s.title, url=s.url, published_at=s.published_at)
                for s in result.sample
            ],
        )
