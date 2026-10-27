"""Request and response bodies of the identity endpoints."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pharos.identity.service import LocalLoginMode


class OidcConfig(BaseModel):
    provider_name: str
    login_url: str


class AuthConfig(BaseModel):
    """What the sign-in page needs to know. Public."""

    local_login: LocalLoginMode
    oidc: OidcConfig | None = None


class LogoutOut(BaseModel):
    """Answer for a single sign-on session: where to go to end the provider's session too."""

    end_session_url: str | None


class LoginRequest(BaseModel):
    # Upper bounds only stop absurd bodies; the real length rules are in the service.
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    id: uuid.UUID
    display_name: str
    email: str | None
    role: str
    locale: str | None
    department: str | None
    last_login_at: datetime | None
    onboarding_completed_at: datetime | None


class SessionOut(BaseModel):
    """The signed-in user plus the token the browser must send as `X-CSRF-Token`."""

    user: UserOut
    csrf_token: str


class AdminUserOut(BaseModel):
    """One person as an administrator sees them."""

    id: uuid.UUID
    display_name: str
    email: str | None
    role: str
    status: str
    department: str | None
    last_login_at: datetime | None
    sign_in: str  # "local" or "sso"


class UserListOut(BaseModel):
    items: list[AdminUserOut]
    total: int


class RoleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "editor", "member"]


class StatusChange(BaseModel):
    """`deprovisioned` is set only by the identity provider's sync, never through this API."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "disabled"]
