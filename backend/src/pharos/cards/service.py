"""The Home feed and the interactions.

The profession, level and interests that steer the ranking come from the person's profile and
nowhere else. Interactions are the person's own: they are written and read under their name
(`act_as`) and the database shows nobody else's; the organization sees only the counts of
`pharos_interaction_counts` (ADR-0018). Like the other services, this one has no network in it.
"""

import uuid
from urllib.parse import urlsplit

from pharos.cards import cursor as cursors
from pharos.cards import repository
from pharos.cards.ranking import (
    MAX_PAGE_SIZE,
    PAGE_SIZE,
    categories_for_interests,
    categories_for_level,
)
from pharos.cards.schemas import (
    CardOut,
    CardPageOut,
    CountOut,
    InteractionKind,
    InteractionSummaryOut,
    Undoable,
)
from pharos.core.clock import Clock
from pharos.core.errors import ApiError, ErrorCode, not_found
from pharos.core.principal import Principal, Role
from pharos.core.ratelimit import RateLimiter, rate_limited
from pharos.core.unit_of_work import UnitOfWork
from pharos.profile.service import ProfileService

INTERACTIONS_PER_HOUR = 600
CARDS_PER_HOUR = 300  # pages of 20: far more than reading needs
SUMMARY_DAYS = 30
OPPOSITE = {"useful": "not_useful", "not_useful": "useful"}


def _invalid(field: str, kind: str) -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_FAILED, 422, errors=[{"loc": ["query", field], "type": kind}]
    )


class CardService:
    def __init__(
        self, *, profile: ProfileService, clock: Clock, limiter: RateLimiter | None = None
    ) -> None:
        self._profile = profile
        self._clock = clock
        self._limiter = limiter

    async def cards(
        self,
        uow: UnitOfWork,
        actor: Principal,
        *,
        cursor: str | None,
        limit: int | None,
        accept_language: str | None,
    ) -> CardPageOut:
        size = min(max(limit or PAGE_SIZE, 1), MAX_PAGE_SIZE)
        await self._limit(uow, actor, "cards", CARDS_PER_HOUR)
        profile = await self._profile.get(uow, actor.user_id, accept_language)
        professions = await self._profile.professions(uow)
        code = next((p.code for p in professions if p.id == profile.profession_id), None)
        if code is None:
            raise ApiError(ErrorCode.ONBOARDING_INCOMPLETE, 409)
        now = self._clock.now()
        after: tuple[int, uuid.UUID] | None = None
        as_of = now
        if cursor is not None:
            parsed = cursors.decode(cursor, now=now)
            if parsed is None:
                raise _invalid("cursor", "invalid_cursor")
            as_of, after = parsed.a, (parsed.s, parsed.i)
        await repository.act_as(uow.session, actor.user_id)
        rows = await repository.ranked_cards(
            uow.session,
            uow.org_id,
            actor.user_id,
            profession=code,
            interest_categories=categories_for_interests(profile.interests),
            level_categories=categories_for_level(profile.ai_level),
            as_of=as_of,
            after=after,
            limit=size + 1,
        )
        page = rows[:size]
        reviewer = actor.role in (Role.ADMIN, Role.EDITOR)
        return CardPageOut(
            items=[
                CardOut(
                    id=r.id,
                    url=r.url,
                    host=urlsplit(r.url).hostname or "",
                    title=r.title,
                    published_at=r.published_at,
                    summary=r.summary,
                    category=r.category,
                    saved=r.saved,
                    quality=r.quality if reviewer else None,
                )
                for r in page
            ],
            next_cursor=(
                cursors.encode(as_of, page[-1].score, page[-1].id)
                if len(rows) > size and page
                else None
            ),
        )

    async def _limit(
        self, uow: UnitOfWork, actor: Principal, what: str = "interactions", limit: int = 0
    ) -> None:
        if self._limiter is None:
            return
        decision = await self._limiter.hit(
            f"{what}:{uow.org_id}:{actor.user_id}",
            limit=limit or INTERACTIONS_PER_HOUR,
            window_seconds=3600,
        )
        if not decision.allowed:
            raise rate_limited(decision.retry_after)

    async def put(
        self,
        uow: UnitOfWork,
        actor: Principal,
        item_id: uuid.UUID,
        kind: InteractionKind,
        value: int | None,
    ) -> None:
        if (kind == "time_saved") != (value is not None):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                422,
                errors=[{"loc": ["body", "value"], "type": "value_not_allowed_or_missing"}],
            )
        await self._limit(uow, actor)
        if not await repository.item_is_readable(uow.session, uow.org_id, item_id):
            raise not_found()
        await repository.act_as(uow.session, actor.user_id)
        opposite = OPPOSITE.get(kind)
        if opposite is not None:
            await repository.delete_interaction(
                uow.session, uow.org_id, actor.user_id, item_id, opposite
            )
        await repository.put_interaction(
            uow.session, uow.org_id, actor.user_id, item_id, kind, value
        )
        await uow.commit()

    async def remove(
        self, uow: UnitOfWork, actor: Principal, item_id: uuid.UUID, kind: Undoable
    ) -> None:
        await self._limit(uow, actor)
        await repository.act_as(uow.session, actor.user_id)
        await repository.delete_interaction(uow.session, uow.org_id, actor.user_id, item_id, kind)
        await uow.commit()

    async def summary(self, uow: UnitOfWork) -> InteractionSummaryOut:
        found = await repository.counts(uow.session, SUMMARY_DAYS)
        return InteractionSummaryOut(
            days=SUMMARY_DAYS,
            counts=[
                CountOut(
                    day=c.day,
                    type=c.type,  # type: ignore[arg-type]
                    people=c.people,
                    actions=c.actions,
                    minutes=c.minutes,
                )
                for c in found
            ],
        )
