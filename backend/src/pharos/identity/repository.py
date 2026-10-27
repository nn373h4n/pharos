"""SQL for local accounts. Every statement runs inside an organization-scoped transaction (RLS)."""

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

LOCAL_ISSUER = "local"
_USERNAME = re.compile(r"[a-z0-9][a-z0-9._@+-]{0,127}")


def normalize_username(raw: str) -> str:
    """Case-folded, NFKC-normalized, trimmed. Lookups and new accounts both go through this."""
    return unicodedata.normalize("NFKC", raw).strip().casefold()


def is_valid_username(username: str) -> bool:
    return _USERNAME.fullmatch(username) is not None


@dataclass(frozen=True, slots=True)
class LocalUser:
    id: uuid.UUID
    role: str
    status: str
    password_hash: str | None


@dataclass(frozen=True, slots=True)
class UserView:
    id: uuid.UUID
    display_name: str
    email: str | None
    role: str
    locale: str | None
    department: str | None
    last_login_at: datetime | None
    onboarding_completed_at: datetime | None


async def find_local_user(
    session: AsyncSession, org_id: uuid.UUID, username: str
) -> LocalUser | None:
    row = (
        await session.execute(
            text(
                "SELECT u.id, u.role, u.status, c.password_hash "
                "FROM users u LEFT JOIN local_credentials c "
                "ON c.org_id = u.org_id AND c.user_id = u.id "
                "WHERE u.org_id = :org AND u.issuer = :issuer AND u.subject = :subject"
            ),
            {"org": org_id, "issuer": LOCAL_ISSUER, "subject": username},
        )
    ).one_or_none()
    return None if row is None else LocalUser(row.id, row.role, row.status, row.password_hash)


async def create_local_user(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    username: str,
    display_name: str,
    email: str | None,
    role: str,
    password_hash: str,
) -> uuid.UUID:
    user_id: uuid.UUID = (
        await session.execute(
            text(
                "INSERT INTO users (org_id, issuer, subject, email, display_name, role) "
                "VALUES (:org, :issuer, :subject, :email, :name, :role) RETURNING id"
            ),
            {
                "org": org_id,
                "issuer": LOCAL_ISSUER,
                "subject": username,
                "email": email,
                "name": display_name,
                "role": role,
            },
        )
    ).scalar_one()
    await session.execute(
        text(
            "INSERT INTO local_credentials (user_id, org_id, password_hash) "
            "VALUES (:user, :org, :hash)"
        ),
        {"user": user_id, "org": org_id, "hash": password_hash},
    )
    return user_id


async def set_password_hash(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, password_hash: str
) -> bool:
    """Replace a hash (password change or rehash). False when the user has no local credential."""
    result = await session.execute(
        text(
            "UPDATE local_credentials SET password_hash = :hash, password_changed_at = now() "
            "WHERE org_id = :org AND user_id = :user"
        ),
        {"org": org_id, "user": user_id, "hash": password_hash},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def rehash_password(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, password_hash: str
) -> None:
    """Store a stronger hash of the SAME password; the change date stays."""
    await session.execute(
        text(
            "UPDATE local_credentials SET password_hash = :hash "
            "WHERE org_id = :org AND user_id = :user"
        ),
        {"org": org_id, "user": user_id, "hash": password_hash},
    )


async def touch_last_login(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    await session.execute(
        text("UPDATE users SET last_login_at = now() WHERE org_id = :org AND id = :user"),
        {"org": org_id, "user": user_id},
    )


async def get_user_view(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> UserView | None:
    row = (
        await session.execute(
            text(
                "SELECT u.id, u.display_name, u.email, u.role, u.locale, d.name AS department, "
                "u.last_login_at, p.onboarding_completed_at FROM users u "
                "LEFT JOIN departments d ON d.org_id = u.org_id AND d.id = u.department_id "
                "LEFT JOIN user_profiles p ON p.org_id = u.org_id AND p.user_id = u.id "
                "WHERE u.org_id = :org AND u.id = :user"
            ),
            {"org": org_id, "user": user_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return UserView(
        row.id,
        row.display_name,
        row.email,
        row.role,
        row.locale,
        row.department,
        row.last_login_at,
        row.onboarding_completed_at,
    )


async def find_user_id_by_username(
    session: AsyncSession, org_id: uuid.UUID, username: str
) -> uuid.UUID | None:
    found = (
        await session.execute(
            text(
                "SELECT id FROM users "
                "WHERE org_id = :org AND issuer = :issuer AND subject = :subject"
            ),
            {"org": org_id, "issuer": LOCAL_ISSUER, "subject": username},
        )
    ).scalar_one_or_none()
    return found if found is None else uuid.UUID(str(found))


@dataclass(frozen=True, slots=True)
class SignedInUser:
    id: uuid.UUID
    role: str
    status: str
    created: bool


async def upsert_oidc_user(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    issuer: str,
    subject: str,
    email: str | None,
    display_name: str | None,
) -> SignedInUser:
    """Find the user by (organization, issuer, subject) or create them. Never by e-mail.

    One atomic statement, so two first sign-ins at the same moment create one user. Role and status
    are not touched on an existing user: a disabled account stays disabled, an administrator stays
    one. A missing e-mail or name in the token does not erase what is stored.
    """
    row = (
        await session.execute(
            text(
                "INSERT INTO users (org_id, issuer, subject, email, display_name) "
                "VALUES (:org, :issuer, :subject, :email, COALESCE(:name, '')) "
                "ON CONFLICT (org_id, issuer, subject) DO UPDATE SET "
                "email = COALESCE(EXCLUDED.email, users.email), "
                "display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), users.display_name) "
                "RETURNING id, role, status, (xmax = 0) AS created"
            ),
            {
                "org": org_id,
                "issuer": issuer,
                "subject": subject,
                "email": email,
                "name": display_name,
            },
        )
    ).one()
    return SignedInUser(row.id, row.role, row.status, bool(row.created))


async def apply_idp_department(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, department: str
) -> None:
    """Put the user in the department the provider names, unless they chose their own."""
    department_id = (
        await session.execute(
            text(
                "INSERT INTO departments (org_id, name) VALUES (:org, :name) "
                "ON CONFLICT (org_id, normalized_name) DO UPDATE SET name = departments.name "
                "RETURNING id"
            ),
            {"org": org_id, "name": department},
        )
    ).scalar_one()
    await session.execute(
        text(
            "UPDATE users SET department_id = :department, department_source = 'idp' "
            "WHERE org_id = :org AND id = :user "
            "AND (department_source IS NULL OR department_source = 'idp')"
        ),
        {"org": org_id, "user": user_id, "department": department_id},
    )


@dataclass(frozen=True, slots=True)
class AdminUserView:
    id: uuid.UUID
    display_name: str
    email: str | None
    role: str
    status: str
    department: str | None
    last_login_at: datetime | None
    sign_in: str  # "local" or "sso"


@dataclass(frozen=True, slots=True)
class UserState:
    id: uuid.UUID
    role: str
    status: str


_ADMIN_VIEW = (
    "SELECT u.id, u.display_name, u.email, u.role, u.status, d.name AS department, "
    "u.last_login_at, (u.issuer = :local) AS is_local FROM users u "
    "LEFT JOIN departments d ON d.org_id = u.org_id AND d.id = u.department_id"
)


def _admin_view(row: Row[Any]) -> AdminUserView:
    return AdminUserView(
        row.id,
        row.display_name,
        row.email,
        row.role,
        row.status,
        row.department,
        row.last_login_at,
        "local" if row.is_local else "sso",
    )


async def list_users(
    session: AsyncSession, org_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[AdminUserView], int]:
    rows = (
        await session.execute(
            text(
                f"{_ADMIN_VIEW} WHERE u.org_id = :org "
                "ORDER BY lower(u.display_name), u.id LIMIT :limit OFFSET :offset"
            ),
            {"org": org_id, "local": LOCAL_ISSUER, "limit": limit, "offset": offset},
        )
    ).all()
    total = (
        await session.execute(
            text("SELECT count(*) FROM users WHERE org_id = :org"), {"org": org_id}
        )
    ).scalar_one()
    return [_admin_view(r) for r in rows], int(total)


async def lock_active_admins(session: AsyncSession, org_id: uuid.UUID) -> set[uuid.UUID]:
    """Lock every active administrator row, always in the same order, and return their ids.

    Two administrators changing each other at the same moment queue up here, so the second one
    decides on what the first one left behind (and cannot leave the organization without an
    administrator)."""
    rows = await session.execute(
        text(
            "SELECT id FROM users WHERE org_id = :org AND role = 'admin' AND status = 'active' "
            "ORDER BY id FOR NO KEY UPDATE"
        ),
        {"org": org_id},
    )
    return {r.id for r in rows}


async def lock_user(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> UserState | None:
    row = (
        await session.execute(
            text(
                "SELECT id, role, status FROM users WHERE org_id = :org AND id = :id "
                "FOR NO KEY UPDATE"
            ),
            {"org": org_id, "id": user_id},
        )
    ).one_or_none()
    return None if row is None else UserState(row.id, row.role, row.status)


async def set_role(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, role: str) -> None:
    await session.execute(
        text("UPDATE users SET role = :role WHERE org_id = :org AND id = :id"),
        {"org": org_id, "id": user_id, "role": role},
    )


async def set_status(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, status: str
) -> None:
    await session.execute(
        text("UPDATE users SET status = :status WHERE org_id = :org AND id = :id"),
        {"org": org_id, "id": user_id, "status": status},
    )


async def get_admin_view(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> AdminUserView | None:
    row = (
        await session.execute(
            text(f"{_ADMIN_VIEW} WHERE u.org_id = :org AND u.id = :id"),
            {"org": org_id, "local": LOCAL_ISSUER, "id": user_id},
        )
    ).one_or_none()
    return None if row is None else _admin_view(row)
