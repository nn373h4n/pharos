"""Request and response bodies of the personal feed endpoints."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MIN_INTERVAL = 3600  # a personal feed is fetched at most hourly (the database enforces it too)
MAX_INTERVAL = 7 * 24 * 3600
MAX_URL = 2048

ItemStatus = Literal["new", "held", "hidden"]


class FeedIn(BaseModel):
    """A new personal feed. It is fetched from the next hour on unless switched off."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=MAX_URL)
    interval_seconds: int = Field(default=MIN_INTERVAL, ge=MIN_INTERVAL, le=MAX_INTERVAL)
    enabled: bool = True


class FeedPatch(BaseModel):
    """Changes to a personal feed. Only the fields that are sent change."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=MAX_URL)
    interval_seconds: int | None = Field(default=None, ge=MIN_INTERVAL, le=MAX_INTERVAL)
    enabled: bool | None = None


class FeedOut(BaseModel):
    """What the owner sees. Nothing about leases, validators or who else exists."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    url: str
    enabled: bool
    interval_seconds: int
    next_fetch_at: datetime
    last_fetched_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


class FeedListOut(BaseModel):
    items: list[FeedOut]
    total: int


class FeedRunOut(BaseModel):
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


class FeedRunListOut(BaseModel):
    items: list[FeedRunOut]


class FeedItemOut(BaseModel):
    """One article of the feed. The text is plain text and `url` an `http(s)` address; the page
    behind it was never fetched. Nobody reviews a personal article: `hold_reason` only tells the
    owner why a rule flagged it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    title: str
    excerpt: str
    lang: str | None
    published_at: datetime | None
    fetched_at: datetime
    status: ItemStatus
    hold_reason: str | None


class FeedItemListOut(BaseModel):
    items: list[FeedItemOut]
    total: int
