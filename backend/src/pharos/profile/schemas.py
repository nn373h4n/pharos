"""Request and response bodies of the profile endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProfileIn(BaseModel):
    """Fields that are present are set (null clears them); absent fields stay as they are."""

    model_config = ConfigDict(extra="forbid")

    profession_id: uuid.UUID | None = None
    # Validated against the fixed catalogue in the service, so the error names the field.
    ai_level: str | None = Field(default=None, max_length=32)
    interests: list[str] | None = Field(default=None, max_length=32)
    department_id: uuid.UUID | None = None
    locale: str | None = Field(default=None, max_length=8)


class ProfileOut(BaseModel):
    profession_id: uuid.UUID | None
    ai_level: str | None
    interests: list[str]
    department_id: uuid.UUID | None
    locale: str | None
    effective_locale: str
    onboarding_started_at: datetime | None
    onboarding_completed_at: datetime | None


class ProfileOptions(BaseModel):
    ai_levels: list[str]
    interests: list[str]
    locales: list[str]


class ProfessionOut(BaseModel):
    id: uuid.UUID
    code: str
    names: dict[str, str]


class DepartmentOut(BaseModel):
    id: uuid.UUID
    name: str
