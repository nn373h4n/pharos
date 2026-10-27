"""SQL for local accounts. Every statement runs inside an organization-scoped transaction (RLS)."""

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
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
