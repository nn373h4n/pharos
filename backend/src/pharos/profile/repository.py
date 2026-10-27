"""SQL for profiles, professions and departments. Statements carry an explicit `org_id` filter
in addition to row-level security."""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ProfileRow:
    profession_id: uuid.UUID | None
    ai_level: str | None
    interests: list[str]
    department_id: uuid.UUID | None
    locale: str | None
    onboarding_started_at: datetime | None
    onboarding_completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProfessionRow:
    id: uuid.UUID
    code: str
    names: dict[str, str]


@dataclass(frozen=True, slots=True)
class DepartmentRow:
    id: uuid.UUID
    name: str


async def get_profile(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> ProfileRow:
    """The user's profile; an empty one when none has been saved yet."""
    row = (
        await session.execute(
            text(
                "SELECT u.department_id, u.locale, p.profession_id, p.ai_level, p.interests, "
                "p.onboarding_started_at, p.onboarding_completed_at "
                "FROM users u LEFT JOIN user_profiles p "
                "ON p.org_id = u.org_id AND p.user_id = u.id "
                "WHERE u.org_id = :org AND u.id = :user"
            ),
            {"org": org_id, "user": user_id},
        )
    ).one()
    return ProfileRow(
        profession_id=row.profession_id,
        ai_level=row.ai_level,
        interests=list(row.interests or []),
        department_id=row.department_id,
        locale=row.locale,
        onboarding_started_at=row.onboarding_started_at,
        onboarding_completed_at=row.onboarding_completed_at,
    )


async def save_profile(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    profession_id: uuid.UUID | None,
    ai_level: str | None,
    interests: list[str],
) -> None:
    """Create or replace the profile fields. The first save sets the start of onboarding."""
    await session.execute(
        text(
            "INSERT INTO user_profiles (user_id, org_id, profession_id, ai_level, interests, "
            "onboarding_started_at) VALUES (:user, :org, :profession, :level, "
            "CAST(:interests AS jsonb), now()) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET "
            "profession_id = EXCLUDED.profession_id, ai_level = EXCLUDED.ai_level, "
            "interests = EXCLUDED.interests, "
            "onboarding_started_at = COALESCE(user_profiles.onboarding_started_at, now())"
        ),
        {
            "user": user_id,
            "org": org_id,
            "profession": profession_id,
            "level": ai_level,
            "interests": json.dumps(interests),
        },
    )


async def set_user_choices(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    locale: str | None,
    department_id: uuid.UUID | None,
    set_department: bool,
    set_locale: bool,
) -> None:
    """The language and department live on the user row. A department the person picked is marked
    as theirs, so a later sign-in through the identity provider does not overwrite it."""
    assignments = []
    params: dict[str, object] = {"org": org_id, "user": user_id}
    if set_locale:
        assignments.append("locale = :locale")
        params["locale"] = locale
    if set_department:
        assignments.append("department_id = :department")
        assignments.append(
            "department_source = CASE WHEN CAST(:department AS uuid) IS NULL "
            "THEN NULL ELSE 'user' END"
        )
        params["department"] = department_id
    if assignments:
        await session.execute(
            text(f"UPDATE users SET {', '.join(assignments)} WHERE org_id = :org AND id = :user"),  # noqa: S608  (fixed fragments only)
            params,
        )


async def complete_onboarding(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> datetime:
    """Stamp the completion once; calling it again changes nothing."""
    stamped = (
        await session.execute(
            text(
                "UPDATE user_profiles SET "
                "onboarding_completed_at = COALESCE(onboarding_completed_at, now()) "
                "WHERE org_id = :org AND user_id = :user RETURNING onboarding_completed_at"
            ),
            {"org": org_id, "user": user_id},
        )
    ).scalar_one()
    return stamped  # type: ignore[no-any-return]


async def list_professions(session: AsyncSession, org_id: uuid.UUID) -> list[ProfessionRow]:
    """Public professions and the organization's own."""
    rows = await session.execute(
        text(
            "SELECT id, code, names FROM professions "
            "WHERE scope = 'public' OR owner_org_id = :org ORDER BY names ->> 'en', code"
        ),
        {"org": org_id},
    )
    return [ProfessionRow(r.id, r.code, dict(r.names)) for r in rows]


async def profession_exists(
    session: AsyncSession, org_id: uuid.UUID, profession_id: uuid.UUID
) -> bool:
    found = await session.execute(
        text(
            "SELECT 1 FROM professions WHERE id = :id AND (scope = 'public' OR owner_org_id = :org)"
        ),
        {"id": profession_id, "org": org_id},
    )
    return found.first() is not None


async def list_departments(session: AsyncSession, org_id: uuid.UUID) -> list[DepartmentRow]:
    rows = await session.execute(
        text("SELECT id, name FROM departments WHERE org_id = :org ORDER BY normalized_name"),
        {"org": org_id},
    )
    return [DepartmentRow(r.id, r.name) for r in rows]


async def department_exists(
    session: AsyncSession, org_id: uuid.UUID, department_id: uuid.UUID
) -> bool:
    found = await session.execute(
        text("SELECT 1 FROM departments WHERE org_id = :org AND id = :id"),
        {"org": org_id, "id": department_id},
    )
    return found.first() is not None
