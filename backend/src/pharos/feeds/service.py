"""A person's own feeds.

The rules of `pharos.sources` apply to the address (`pharos.ingest.sourceconfig`: nothing inside the
network, no credentials, no odd spellings of an address), with two differences that suit a feed one
person owns: only RSS and Atom, and an interval of an hour or more. At most five feeds per person;
nobody else, not even an administrator, can see or change them.

Like the sources service this one has no network in it and imports nothing that has (an
import-linter contract keeps it so). A refusal that matters (an address pointing inwards, a sixth
feed, a duplicate) leaves a trace in the audit log, without the address in it.
"""

import uuid
from collections.abc import Collection
from typing import Any, NoReturn

from sqlalchemy.exc import IntegrityError

from pharos.core.audit import ActorType, AuditAction, AuditLog, Outcome
from pharos.core.errors import ApiError, ErrorCode, not_found
from pharos.core.principal import Principal
from pharos.core.ratelimit import RateLimiter, rate_limited
from pharos.core.unit_of_work import UnitOfWork
from pharos.feeds import repository
from pharos.feeds.schemas import FeedIn, FeedPatch
from pharos.ingest.sourceconfig import ConfigError, validate
from pharos.ingest.text import clean_text

MAX_FEEDS_PER_PERSON = 5
NAME_LIMIT = 200
# A new address is fetched within about a minute (the first fetch is due at once), so a person
# who adds and deletes feeds could make the worker fetch addresses of their choice. This bounds
# how many new addresses (a feed added, or the address of one changed) a person can bring in.
NEW_ADDRESSES_PER_HOUR = 10


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


class FeedService:
    def __init__(
        self,
        *,
        audit: AuditLog,
        blocked_hosts: Collection[str],
        available: bool = True,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._audit = audit
        self._limiter = limiter
        self._blocked_hosts = tuple(blocked_hosts)
        # Switched off by the operator (PHAROS_PERSONAL_FEEDS=off): the endpoints do not exist.
        self._available = available

    @property
    def available(self) -> bool:
        return self._available

    async def _begin(self, uow: UnitOfWork, actor: Principal) -> None:
        if not self._available:
            raise not_found()
        await repository.act_as(uow.session, actor.user_id)

    # --- reading ---------------------------------------------------------------------------------

    async def list_feeds(
        self, uow: UnitOfWork, actor: Principal, *, limit: int, offset: int
    ) -> tuple[list[repository.FeedView], int]:
        await self._begin(uow, actor)
        return await repository.list_feeds(
            uow.session, uow.org_id, actor.user_id, limit=limit, offset=offset
        )

    async def get(
        self, uow: UnitOfWork, actor: Principal, feed_id: uuid.UUID
    ) -> repository.FeedView:
        await self._begin(uow, actor)
        view = await repository.get_feed(uow.session, uow.org_id, actor.user_id, feed_id)
        if view is None:
            raise not_found()  # missing, or somebody else's: the same answer
        return view

    async def runs(
        self, uow: UnitOfWork, actor: Principal, feed_id: uuid.UUID, *, limit: int
    ) -> list[Any]:
        await self.get(uow, actor, feed_id)
        return await repository.recent_runs(
            uow.session, uow.org_id, actor.user_id, feed_id, limit=limit
        )

    async def items(
        self, uow: UnitOfWork, actor: Principal, feed_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[Any], int]:
        await self.get(uow, actor, feed_id)
        return await repository.list_items(
            uow.session, uow.org_id, actor.user_id, feed_id, limit=limit, offset=offset
        )

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
    ) -> NoReturn:
        """Refuse, and leave a trace in a transaction of its own (this one is rolled back). The
        trace holds the reason and that the feed is personal, never the address."""
        await uow.release()
        await self._audit.record_independently(
            org_id=uow.org_id,
            action=action,
            outcome=Outcome.FAILURE,
            actor_type=ActorType.USER,
            actor_id=actor.user_id,
            target=target,
            ip=ip,
            details={"reason": reason, "scope": "personal", "type": "rss"},
        )
        raise error

    async def _new_address(
        self,
        uow: UnitOfWork,
        actor: Principal,
        action: AuditAction,
        target: str | None,
        ip: str | None,
    ) -> None:
        """Count one new address for this person; refuse (and leave a trace) past the limit."""
        if self._limiter is None:
            return
        decision = await self._limiter.hit(
            f"feeds:{uow.org_id}:{actor.user_id}", limit=NEW_ADDRESSES_PER_HOUR, window_seconds=3600
        )
        if not decision.allowed:
            await self._refuse(
                uow,
                actor,
                action,
                target,
                ip,
                "rate",
                rate_limited(decision.retry_after),
            )

    async def _validated(
        self,
        uow: UnitOfWork,
        actor: Principal,
        action: AuditAction,
        target: str | None,
        ip: str | None,
        url: str,
    ) -> tuple[dict[str, Any], str]:
        try:
            checked = validate("rss", {"url": url}, blocked_hosts=self._blocked_hosts)
        except ConfigError as error:
            if error.code == "url_not_allowed":  # worth a trace: somebody pointed us inwards
                await self._refuse(
                    uow, actor, action, target, ip, error.code, _invalid("url", error.code)
                )
            raise _invalid("url", error.code) from None
        return checked.config, checked.dedupe_key

    async def create(
        self, uow: UnitOfWork, actor: Principal, body: FeedIn, *, ip: str | None
    ) -> repository.FeedView:
        action = AuditAction.SOURCE_CREATED
        await self._begin(uow, actor)
        name = clean_name(body.name)
        config, key = await self._validated(uow, actor, action, None, ip, body.url)
        await self._new_address(uow, actor, action, None, ip)
        await repository.lock_user_feeds(uow.session, uow.org_id, actor.user_id)
        if await repository.count_feeds(uow.session, uow.org_id, actor.user_id) >= (
            MAX_FEEDS_PER_PERSON
        ):
            await self._refuse(
                uow, actor, action, None, ip, "limit", ApiError(ErrorCode.SOURCE_LIMIT, 409)
            )
        try:
            created = await repository.insert_feed(
                uow.session,
                uow.org_id,
                actor.user_id,
                name=name,
                config=config,
                dedupe_key=key,
                enabled=body.enabled,
                interval_seconds=body.interval_seconds,
            )
        except IntegrityError:  # the database's own limit, if another request got in between
            await self._refuse(
                uow, actor, action, None, ip, "limit", ApiError(ErrorCode.SOURCE_LIMIT, 409)
            )
        if created is None:
            await self._refuse(
                uow, actor, action, None, ip, "duplicate", ApiError(ErrorCode.SOURCE_CONFLICT, 409)
            )
        await self._audit.record(
            uow.session,
            org_id=uow.org_id,
            action=action,
            actor_id=actor.user_id,
            target=f"source:{created.id}",
            ip=ip,
            details={
                "scope": "personal",
                "type": "rss",
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
        feed_id: uuid.UUID,
        body: FeedPatch,
        *,
        ip: str | None,
    ) -> repository.FeedView:
        await self._begin(uow, actor)
        current = await repository.get_feed(
            uow.session, uow.org_id, actor.user_id, feed_id, lock=True
        )
        if current is None:
            raise not_found()
        target = f"source:{feed_id}"
        given = body.model_fields_set
        await repository.lock_user_feeds(uow.session, uow.org_id, actor.user_id)

        name = clean_name(body.name) if "name" in given and body.name is not None else current.name
        interval = body.interval_seconds if "interval_seconds" in given else None
        interval = current.interval_seconds if interval is None else interval
        enabled = (
            body.enabled if "enabled" in given and body.enabled is not None else current.enabled
        )
        config: dict[str, Any] = {"url": current.url}
        key = current.dedupe_key
        if "url" in given and body.url is not None:
            config, key = await self._validated(
                uow, actor, AuditAction.SOURCE_UPDATED, target, ip, body.url
            )
        elif enabled and not current.enabled:
            # Switching a feed on is when it starts to cost something: look at its address again,
            # against what is known now.
            await self._validated(uow, actor, AuditAction.SOURCE_UPDATED, target, ip, current.url)
        if key != current.dedupe_key and await repository.key_taken(
            uow.session, uow.org_id, actor.user_id, key, except_id=feed_id
        ):
            await self._refuse(
                uow,
                actor,
                AuditAction.SOURCE_UPDATED,
                target,
                ip,
                "duplicate",
                ApiError(ErrorCode.SOURCE_CONFLICT, 409),
            )

        if key != current.dedupe_key:
            await self._new_address(uow, actor, AuditAction.SOURCE_UPDATED, target, ip)

        changed = [
            field
            for field, before, after in (
                ("name", current.name, name),
                ("url", current.url, config["url"]),
                ("interval_seconds", current.interval_seconds, interval),
            )
            if before != after
        ]
        flipped = enabled != current.enabled
        if not changed and not flipped:
            return current

        try:
            updated = await repository.update_feed(
                uow.session,
                uow.org_id,
                actor.user_id,
                feed_id,
                name=name,
                config=config,
                dedupe_key=key,
                enabled=enabled,
                interval_seconds=interval,
            )
        except IntegrityError:  # the same address was taken in the meantime
            await self._refuse(
                uow,
                actor,
                AuditAction.SOURCE_UPDATED,
                target,
                ip,
                "duplicate",
                ApiError(ErrorCode.SOURCE_CONFLICT, 409),
            )
        if changed:
            await self._audit.record(
                uow.session,
                org_id=uow.org_id,
                action=AuditAction.SOURCE_UPDATED,
                actor_id=actor.user_id,
                target=target,
                ip=ip,
                details={"scope": "personal", "type": "rss", "fields": ",".join(changed)},
            )
        if flipped:
            await self._audit.record(
                uow.session,
                org_id=uow.org_id,
                action=AuditAction.SOURCE_ENABLED if enabled else AuditAction.SOURCE_DISABLED,
                actor_id=actor.user_id,
                target=target,
                ip=ip,
                details={"scope": "personal", "type": "rss"},
            )
        await uow.commit()
        return updated

    async def delete(
        self, uow: UnitOfWork, actor: Principal, feed_id: uuid.UUID, *, ip: str | None
    ) -> None:
        await self._begin(uow, actor)
        current = await repository.get_feed(
            uow.session, uow.org_id, actor.user_id, feed_id, lock=True
        )
        if current is None:
            raise not_found()
        await repository.delete_feed(uow.session, uow.org_id, actor.user_id, feed_id)
        await self._audit.record(
            uow.session,
            org_id=uow.org_id,
            action=AuditAction.SOURCE_DELETED,
            actor_id=actor.user_id,
            target=f"source:{feed_id}",
            ip=ip,
            details={"scope": "personal", "type": "rss"},
        )
        await uow.commit()
