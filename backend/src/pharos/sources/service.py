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

import uuid
from collections.abc import Collection
from typing import Any, NoReturn

from sqlalchemy.exc import IntegrityError

from pharos.core.audit import ActorType, AuditAction, AuditLog, Outcome
from pharos.core.errors import ApiError, ErrorCode, not_found
from pharos.core.principal import Principal
from pharos.core.unit_of_work import UnitOfWork
from pharos.ingest.sourceconfig import ConfigError, validate
from pharos.ingest.text import clean_text
from pharos.sources import repository
from pharos.sources.schemas import SourceIn, SourcePatch

MAX_ORG_SOURCES = 200
NAME_LIMIT = 200


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
    def __init__(self, *, audit: AuditLog, blocked_hosts: Collection[str]) -> None:
        self._audit = audit
        self._blocked_hosts = tuple(blocked_hosts)

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
