"""The SQL of the Home feed and of interactions.

The feed ranks in the database at one fixed moment (`as_of`), see `pharos.cards.ranking`. Every
statement names the organization and the person itself; row-level security is the second line
(interactions are private to the person by a policy, ADR-0018).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pharos.cards.ranking import HALF_LIFE_DAYS, SCORE_SCALE, WEIGHTS, WINDOW_DAYS


@dataclass(frozen=True, slots=True)
class CardRow:
    id: uuid.UUID
    url: str
    title: str
    published_at: datetime | None
    fetched_at: datetime
    summary: str
    category: str
    quality: int
    saved: bool
    score: int


@dataclass(frozen=True, slots=True)
class CountRow:
    day: date
    type: str
    people: int
    actions: int
    minutes: int | None


async def act_as(session: AsyncSession, user: uuid.UUID) -> None:
    """Name the person for this transaction: the database then shows their interactions only.
    It lasts until the transaction ends; call it again after a commit or a release."""
    await session.execute(
        text("SELECT set_config('app.user_id', :user, true)"), {"user": str(user)}
    )


_RANKED = f"""
SELECT i.id, i.canonical_url AS url, i.title, i.published_at, i.fetched_at, i.summary,
       i.category, i.quality,
       EXISTS (SELECT 1 FROM interactions s WHERE s.org_id = i.org_id AND s.user_id = :user
               AND s.item_id = i.id AND s.type = 'save') AS saved,
       floor({SCORE_SCALE} * (
           CAST(:w_relevance AS numeric) * coalesce(p.relevance, 0) / 100.0
         + CAST(:w_recency AS numeric) * power(0.5, greatest(0, extract(epoch FROM (CAST(:as_of AS timestamptz)
               - coalesce(i.published_at, i.fetched_at))) / 86400.0) / {HALF_LIFE_DAYS})
         + CAST(:w_quality AS numeric) * i.quality / 5.0
         + CAST(:w_interest AS numeric) * (CASE WHEN i.category = ANY(CAST(:interest_categories AS text[]))
                               THEN 1 ELSE 0 END)
         + CAST(:w_level AS numeric) * (CASE WHEN i.category = ANY(CAST(:level_categories AS text[]))
                            THEN 1 ELSE 0 END)
       ))::bigint AS score
FROM items i
LEFT JOIN item_professions p
  ON p.org_id = i.org_id AND p.item_id = i.id AND p.profession_code = :profession
WHERE i.org_id = :org AND i.owner_user_id IS NULL AND i.status = 'new'
  AND i.enriched_at IS NOT NULL AND i.summary IS NOT NULL
  AND coalesce(i.published_at, i.fetched_at) <= CAST(:as_of AS timestamptz)
  AND coalesce(i.published_at, i.fetched_at)
      > CAST(:as_of AS timestamptz) - interval '{WINDOW_DAYS} days'
  AND NOT EXISTS (SELECT 1 FROM interactions n WHERE n.org_id = i.org_id AND n.user_id = :user
                  AND n.item_id = i.id AND n.type = 'not_useful')
"""  # noqa: S608, E501 (the numbers are module constants; a SQL text)


async def ranked_cards(
    session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    *,
    profession: str,
    interest_categories: list[str],
    level_categories: list[str],
    as_of: datetime,
    after: tuple[int, uuid.UUID] | None,
    limit: int,
) -> list[CardRow]:
    """The best `limit` cards after `after` (a score and an id), in score order, ties by id."""
    keyset = "WHERE (r.score, r.id) < (:after_score, :after_id) " if after else ""
    rows = await session.execute(
        text(
            f"SELECT * FROM ({_RANKED}) r {keyset}"  # noqa: S608 (fixed fragments only)
            "ORDER BY r.score DESC, r.id DESC LIMIT :limit"
        ),
        {
            "org": org,
            "user": user,
            "profession": profession,
            "as_of": as_of,
            "interest_categories": interest_categories,
            "level_categories": level_categories,
            "limit": limit,
            **{f"w_{name}": weight for name, weight in WEIGHTS.items()},
            **({"after_score": after[0], "after_id": after[1]} if after else {}),
        },
    )
    return [
        CardRow(
            id=r.id,
            url=r.url,
            title=r.title,
            published_at=r.published_at,
            fetched_at=r.fetched_at,
            summary=r.summary,
            category=r.category,
            quality=r.quality,
            saved=r.saved,
            score=int(r.score),
        )
        for r in rows
    ]


async def item_is_readable(session: AsyncSession, org: uuid.UUID, item: uuid.UUID) -> bool:
    """A current organization article: the only kind that has a card and takes interactions."""
    row = await session.execute(
        text(
            "SELECT 1 FROM items WHERE org_id = :org AND id = :id "
            "AND owner_user_id IS NULL AND status = 'new' "
            "AND enriched_at IS NOT NULL AND summary IS NOT NULL"
        ),
        {"org": org, "id": item},
    )
    return row.first() is not None


async def put_interaction(
    session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    item: uuid.UUID,
    kind: str,
    value: int | None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO interactions (org_id, user_id, item_id, type, value) "
            "VALUES (:org, :user, :item, :type, :value) "
            "ON CONFLICT (org_id, user_id, item_id, type) "
            "DO UPDATE SET value = EXCLUDED.value "
            # Minutes may be corrected on the day they were given, not after: a day that has been
            # counted does not change, and a row never moves to another day.
            "WHERE interactions.type = 'time_saved' "
            "AND interactions.created_at::date = now()::date"
        ),
        {"org": org, "user": user, "item": item, "type": kind, "value": value},
    )


async def delete_interaction(
    session: AsyncSession, org: uuid.UUID, user: uuid.UUID, item: uuid.UUID, kind: str
) -> None:
    await session.execute(
        text(
            "DELETE FROM interactions WHERE org_id = :org AND user_id = :user "
            "AND item_id = :item AND type = :type"
        ),
        {"org": org, "user": user, "item": item, "type": kind},
    )


async def counts(session: AsyncSession, days: int) -> list[CountRow]:
    """Only the aggregates the database function allows: complete UTC days, days and kinds with
    fewer than five people left out, the minutes capped per person and rounded."""
    rows = await session.execute(
        text("SELECT day, type, people, actions, minutes FROM pharos_interaction_counts(:days)"),
        {"days": days},
    )
    return [CountRow(r.day, r.type, r.people, r.actions, r.minutes) for r in rows]
