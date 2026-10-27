"""Request and response bodies of the identity endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from pharos.identity.service import LocalLoginMode


class AuthConfig(BaseModel):
    """What the sign-in page needs to know. Public."""

    local_login: LocalLoginMode


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
