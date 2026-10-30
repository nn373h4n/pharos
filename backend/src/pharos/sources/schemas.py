"""Request and response bodies of the source endpoints."""

import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MIN_INTERVAL = 300
MAX_INTERVAL = 7 * 24 * 3600

SourceType = Literal["rss", "github", "hackernews"]


class SourceIn(BaseModel):
    """A new organization source. It starts disabled unless the administrator says otherwise."""

    model_config = ConfigDict(extra="forbid")

    type: SourceType
    name: str = Field(min_length=1, max_length=200)
    config: dict[str, Any]
    interval_seconds: int = Field(default=3600, ge=MIN_INTERVAL, le=MAX_INTERVAL)
    enabled: bool = False


class SourcePatch(BaseModel):
    """Changes to a source. The type never changes: make a new source instead."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict[str, Any] | None = None
    interval_seconds: int | None = Field(default=None, ge=MIN_INTERVAL, le=MAX_INTERVAL)
    enabled: bool | None = None

    @model_validator(mode="after")
    def _says_something(self) -> Self:
        given = self.model_fields_set
        if not given or any(getattr(self, name) is None for name in given):
            raise ValueError("a change needs at least one field and no null values")
        return self


class SourceOut(BaseModel):
    """What an administrator or editor sees. Nothing about leases, validators or credentials."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: SourceType
    name: str
    config: dict[str, Any]
    enabled: bool
    interval_seconds: int
    next_fetch_at: datetime
    last_fetched_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error_code: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class SourceListOut(BaseModel):
    items: list[SourceOut]
    total: int


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    started_at: datetime
    finished_at: datetime | None
    outcome: str
    error_code: str | None
    http_status: int | None
    duration_ms: int | None
    bytes_fetched: int | None
    items_seen: int | None
    items_new: int | None


class RunListOut(BaseModel):
    items: list[RunOut]
