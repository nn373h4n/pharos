"""Retention, and deleting one person's data (M5-09, ADR-0025).

Three operator commands, all run as the TABLE OWNER (the `operator` service of Compose, started on
demand and never by `up`). The application and the worker roles can delete none of this, on
purpose: the audit log is append-only for them, what people do with the cards is private to each
person, and a person's row is theirs to remove only through an operator.

* `delete_person`: removes one person. Their profile, password, what they did with the cards,
  their own feeds and the articles of those feeds go with the row (the foreign keys cascade). What
  does NOT cascade is cleared by hand in the same transaction: the username in the targets of
  earlier audit entries, the address hashes of their audit entries, and the `created_by` marks on
  sources and tools. What stays is the identifier in `actor_id` and in the target of their audit
  entries: it no longer leads to anybody, and it goes when the audit retention ends. The one new
  trace is an audit entry that holds counts and no name. Sessions are ended afterwards (by the
  command line, which knows the session store).
* `prune_audit`: deletes audit entries older than the retention (default one year, never under
  30 days), and leaves an entry that says it did.
* `fold_interactions`: writes the monthly counts that the organization may keep (a month and kind
  with fewer than five people is not kept: the privacy floor of the daily summary) and deletes the
  interactions of months older than the retention (default twelve months, never under three).

Nothing here is reachable from a request. Every command does nothing but report unless it is told
to confirm; a count is shown first.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from pharos.core.audit import ActorType, AuditAction, Outcome, clean_details
from pharos.core.config import MIN_AUDIT_RETENTION_DAYS, MIN_INTERACTIONS_RETENTION_MONTHS

# The privacy floor of the daily summary (migration 0008) and a cap on one person's minutes, here
# per person and month (the daily summary caps each row), written out so the rule does not move.
FLOOR = 5
MINUTES_CAP = 120
MINUTES_STEP = 30
LOCAL_ISSUER = "local"


class RetentionError(Exception):
    """A problem the operator can fix. Printed without a traceback."""


@dataclass(frozen=True, slots=True)
class PersonDeleted:
    user_id: uuid.UUID
    username: str | None
    was_administrator: bool
    interactions: int
    personal_feeds: int
    personal_articles: int
    audit_rows_cleared: int
    marks_cleared: int
    deleted: bool


@dataclass(frozen=True, slots=True)
class AuditPruned:
    cutoff: datetime
    rows: int
    deleted: bool


@dataclass(frozen=True, slots=True)
class InteractionsFolded:
    cutoff: date
    rows: int
    months_kept: int
    months_dropped: int
    deleted: bool


async def _organization(connection: AsyncConnection, slug: str) -> uuid.UUID:
    found = (
        await connection.execute(text("SELECT id FROM organizations WHERE slug = :s"), {"s": slug})
    ).scalar_one_or_none()
    if found is None:
        raise RetentionError(f"the organization {slug!r} does not exist")
    return uuid.UUID(str(found))


async def _audit(
    connection: AsyncConnection,
    org: uuid.UUID,
    action: AuditAction,
    *,
    target: str | None,
    details: dict[str, Any],
) -> None:
    """One audit entry, written as the owner (the application role may only insert too)."""
    await connection.execute(
        text(
            "INSERT INTO audit_logs (org_id, actor_type, action, target, outcome, details) "
            "VALUES (:org, :actor, :action, :target, :outcome, CAST(:details AS jsonb))"
        ),
        {
            "org": org,
            "actor": ActorType.OPERATOR_CLI.value,
            "action": action.value,
            "target": target,
            "outcome": Outcome.SUCCESS.value,
            "details": json.dumps(clean_details(details)),
        },
    )


# --- deleting a person ---


async def _active_administrators(connection: AsyncConnection, org: uuid.UUID) -> list[uuid.UUID]:
    """The active administrators, locked in a fixed order: the same rows, the same order and the
    same kind of lock as the API takes when it changes an administrator, so two of these, or one
    of these and a request, queue up instead of each seeing the other as still there."""
    rows = await connection.execute(
        text(
            "SELECT id FROM users WHERE org_id = :org AND role = 'admin' AND status = 'active' "
            "ORDER BY id FOR NO KEY UPDATE"
        ),
        {"org": org},
    )
    return [uuid.UUID(str(r.id)) for r in rows]


async def delete_person(
    owner_url: str,
    *,
    org_slug: str,
    user_id: uuid.UUID | None = None,
    username: str | None = None,
    confirm: bool = False,
    allow_reprovision: bool = False,
) -> PersonDeleted:
    """Remove one person, or say what removing them would do (`confirm` false).

    Refused for the last active administrator: an installation with no administrator cannot be
    run. Refused for a person who signs in through the identity provider unless
    `allow_reprovision`: their account is made again, empty, at their next sign-in, so a person who
    was disabled for cause would be let back in by the deletion."""
    if (user_id is None) == (username is None):
        raise RetentionError("name the person by exactly one of a user id and a username")
    engine = create_async_engine(owner_url)
    try:
        async with engine.begin() as connection:
            org = await _organization(connection, org_slug)
            params: dict[str, Any]
            if user_id is not None:
                where, params = "id = :v", {"v": user_id}
            else:
                where, params = "issuer = 'local' AND subject = :v", {"v": str(username)}
            row = (
                await connection.execute(
                    text(
                        "SELECT id, role, status, issuer, subject FROM users "  # noqa: S608
                        f"WHERE org_id = :org AND {where}"
                    ),
                    {"org": org, **params},
                )
            ).first()
            if row is None:
                raise RetentionError("there is no such person in this organization")
            person = uuid.UUID(str(row.id))
            local = row.issuer == LOCAL_ISSUER
            if not local and not allow_reprovision:
                raise RetentionError(
                    "this person signs in through the identity provider, which would make their "
                    "account again at their next sign-in (empty, active, and without any block "
                    "set here). Remove or disable them at the identity provider first, then "
                    "repeat with --allow-reprovision"
                )
            administrator = row.role == "admin"
            # Locks first, in one order, so that the answers below cannot change under us: the
            # active administrators, then this person's row.
            admins = await _active_administrators(connection, org)
            if person in admins and not [a for a in admins if a != person]:
                raise RetentionError(
                    "this is the last active administrator: create or promote another first"
                )
            still_there = (
                await connection.execute(
                    text("SELECT 1 FROM users WHERE org_id = :org AND id = :id FOR UPDATE"),
                    {"org": org, "id": person},
                )
            ).first()
            if still_there is None:
                raise RetentionError("there is no such person in this organization")
            named = f"{LOCAL_ISSUER}:{row.subject}" if local else ""
            counts = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM interactions "
                        "WHERE org_id = :org AND user_id = :id), "
                        "(SELECT count(*) FROM sources "
                        "WHERE org_id = :org AND owner_user_id = :id), "
                        "(SELECT count(*) FROM items "
                        "WHERE org_id = :org AND owner_user_id = :id), "
                        "(SELECT count(*) FROM audit_logs WHERE org_id = :org AND "
                        "(target = :named OR (ip_hash IS NOT NULL AND (actor_id = :id "
                        "OR (actor_id IS NULL AND target = :mine))))), "
                        "(SELECT (SELECT count(*) FROM sources WHERE org_id = :org "
                        "AND created_by = :id) + (SELECT count(*) FROM ai_tools "
                        "WHERE org_id = :org AND created_by = :id))"
                    ),
                    {"org": org, "id": person, "named": named, "mine": f"user:{person}"},
                )
            ).one()
            result = PersonDeleted(
                user_id=person,
                username=str(row.subject) if local else None,
                was_administrator=administrator,
                interactions=int(counts[0]),
                personal_feeds=int(counts[1]),
                personal_articles=int(counts[2]),
                audit_rows_cleared=int(counts[3]),
                marks_cleared=int(counts[4]),
                deleted=confirm,
            )
            if not confirm:
                return result
            # What does not cascade, cleared by hand: the username a failed or a good sign-in put
            # into the target of an audit entry, the hash of the address of their own entries, and
            # the `created_by` marks that point at them.
            # The hash first: it is found through the username, which the next statement removes.
            # Refused sign-ins that named the person have no actor but carry the hash too, and so
            # does the entry the system wrote when their first single sign-on made the account
            # (target `user:<id>`, no actor). An entry another person wrote about them keeps its
            # hash: that address is the other person's.
            await connection.execute(
                text(
                    "UPDATE audit_logs SET ip_hash = NULL WHERE org_id = :org "
                    "AND ip_hash IS NOT NULL AND (actor_id = :id OR target = :named "
                    "OR (actor_id IS NULL AND target = :mine))"
                ),
                {"org": org, "id": person, "named": named, "mine": f"user:{person}"},
            )
            if local:
                await connection.execute(
                    text(
                        "UPDATE audit_logs SET target = :gone "
                        "WHERE org_id = :org AND target = :named"
                    ),
                    {"org": org, "gone": f"{LOCAL_ISSUER}:(deleted)", "named": named},
                )
            for table in ("sources", "ai_tools"):
                await connection.execute(
                    text(
                        f"UPDATE {table} SET created_by = NULL "  # noqa: S608 (two fixed names)
                        "WHERE org_id = :org AND created_by = :id"
                    ),
                    {"org": org, "id": person},
                )
            # Everything that belongs to the person goes with this row (ON DELETE CASCADE): the
            # profile, the password, the interactions, the personal feeds and their articles.
            await connection.execute(
                text("DELETE FROM users WHERE org_id = :org AND id = :id"),
                {"org": org, "id": person},
            )
            await _audit(
                connection,
                org,
                AuditAction.USER_DELETED,
                target=f"user:{person}",
                details={
                    "interactions": result.interactions,
                    "personal_feeds": result.personal_feeds,
                    "personal_articles": result.personal_articles,
                    "audit_rows_cleared": result.audit_rows_cleared,
                    "marks_cleared": result.marks_cleared,
                    "was_administrator": administrator,
                },
            )
            return result
    finally:
        await engine.dispose()


# --- the audit log ---


def _utc(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


async def prune_audit(
    owner_url: str,
    *,
    org_slug: str,
    days: int,
    confirm: bool = False,
    now: datetime | None = None,
) -> AuditPruned:
    """Delete the audit entries older than `days` days (strictly older), or count them."""
    if days < MIN_AUDIT_RETENTION_DAYS:
        raise RetentionError(
            f"the audit retention is at least {MIN_AUDIT_RETENTION_DAYS} days: "
            "a shorter one would wipe the trail"
        )
    cutoff = _utc(now) - timedelta(days=days)
    engine = create_async_engine(owner_url)
    try:
        async with engine.begin() as connection:
            org = await _organization(connection, org_slug)
            params = {"org": org, "cutoff": cutoff}
            if not confirm:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM audit_logs "
                            "WHERE org_id = :org AND occurred_at < :cutoff"
                        ),
                        params,
                    )
                ).scalar_one()
                return AuditPruned(cutoff=cutoff, rows=int(rows), deleted=False)
            removed = await connection.execute(
                text("DELETE FROM audit_logs WHERE org_id = :org AND occurred_at < :cutoff"),
                params,
            )
            rows = int(removed.rowcount or 0)
            await _audit(
                connection,
                org,
                AuditAction.AUDIT_PRUNED,
                target=None,
                details={"rows": rows, "older_than_days": days},
            )
            return AuditPruned(cutoff=cutoff, rows=rows, deleted=True)
    finally:
        await engine.dispose()


# --- what people did with the cards ---


def month_cutoff(now: datetime, months: int) -> date:
    """The first day of the month `months` months before `now` (UTC). Interactions before it are
    folded and deleted; the ones from that month on stay, so a row is kept for at least `months`
    months and at most one more."""
    now = now.astimezone(UTC)
    index = now.year * 12 + (now.month - 1) - months
    return date(index // 12, index % 12 + 1, 1)


# One row per (month, kind, person) first, so that the cap on the minutes is a cap on a PERSON and
# not on each of their articles; then one row per (month, kind).
_FOLD = (
    "SELECT month, type, count(*) AS people, sum(actions) AS actions, "
    "CASE WHEN type = 'time_saved' THEN "
    "round(sum(least(minutes, CAST(:cap AS integer))) / CAST(:step AS numeric)) "
    "* CAST(:step AS numeric) END AS minutes "
    "FROM (SELECT date_trunc('month', created_at AT TIME ZONE 'UTC')::date AS month, type, "
    "user_id, count(*) AS actions, sum(value) AS minutes FROM interactions "
    "WHERE org_id = :org AND created_at < :cutoff GROUP BY 1, 2, 3) per_person GROUP BY 1, 2"
)


async def fold_interactions(
    owner_url: str,
    *,
    org_slug: str,
    months: int,
    confirm: bool = False,
    now: datetime | None = None,
) -> InteractionsFolded:
    """Write the monthly counts of the months before the cutoff and delete their interactions.

    Everything happens in one transaction: if a count cannot be written (a month that already has
    one, for example after a restore) nothing is deleted and the command stops with the error."""
    if months < MIN_INTERACTIONS_RETENTION_MONTHS:
        raise RetentionError(
            f"the retention of what people did is at least {MIN_INTERACTIONS_RETENTION_MONTHS} "
            "months"
        )
    cutoff = month_cutoff(_utc(now), months)
    start = datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=UTC)
    engine = create_async_engine(owner_url)
    try:
        async with engine.begin() as connection:
            org = await _organization(connection, org_slug)
            params: dict[str, Any] = {
                "org": org,
                "cutoff": start,
                "cap": MINUTES_CAP,
                "step": MINUTES_STEP,
                "floor": FLOOR,
            }
            rows = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM interactions "
                        "WHERE org_id = :org AND created_at < :cutoff"
                    ),
                    params,
                )
            ).scalar_one()
            kept = (
                await connection.execute(
                    text(f"SELECT count(*) FROM ({_FOLD}) g WHERE people >= :floor"),  # noqa: S608
                    params,
                )
            ).scalar_one()
            groups = (
                await connection.execute(text(f"SELECT count(*) FROM ({_FOLD}) g"), params)  # noqa: S608
            ).scalar_one()
            result = InteractionsFolded(
                cutoff=cutoff,
                rows=int(rows),
                months_kept=int(kept),
                months_dropped=int(groups) - int(kept),
                deleted=confirm,
            )
            if not confirm:
                return result
            try:
                await connection.execute(
                    text(
                        "INSERT INTO interaction_counts_monthly "  # noqa: S608 (fixed query above)
                        "(org_id, month, type, people, actions, minutes) "
                        f"SELECT :org, month, type, people, actions, minutes FROM ({_FOLD}) g "
                        "WHERE people >= :floor"
                    ),
                    params,
                )
            except IntegrityError:
                raise RetentionError(
                    "a monthly count already exists for a month before the cutoff (after a "
                    "restore, for example). Nothing was changed: look at "
                    "interaction_counts_monthly first"
                ) from None
            await connection.execute(
                text("DELETE FROM interactions WHERE org_id = :org AND created_at < :cutoff"),
                {"org": org, "cutoff": start},
            )
            await _audit(
                connection,
                org,
                AuditAction.INTERACTIONS_FOLDED,
                target=None,
                details={
                    "rows": result.rows,
                    "months_kept": result.months_kept,
                    "months_dropped": result.months_dropped,
                    "before": cutoff.isoformat(),
                },
            )
            return result
    finally:
        await engine.dispose()
