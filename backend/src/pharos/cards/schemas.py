"""Request and response bodies of the card endpoints. Every text is plain text."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

InteractionKind = Literal["view", "save", "useful", "not_useful", "tried", "time_saved"]
Undoable = Literal["save", "useful", "not_useful"]


class CardOut(BaseModel):
    id: uuid.UUID
    url: str
    host: str
    title: str
    published_at: datetime | None
    summary: str
    category: str
    saved: bool
    quality: int | None  # reviewers only


class CardPageOut(BaseModel):
    items: list[CardOut]
    next_cursor: str | None


class InteractionIn(BaseModel):
    """`value` is the minutes saved for `time_saved`, and must be left out for every other kind."""

    value: int | None = Field(default=None, ge=1, le=480)


class CountOut(BaseModel):
    day: date
    type: InteractionKind
    people: int
    actions: int
    minutes: int | None  # rounded; only for time_saved


class InteractionSummaryOut(BaseModel):
    days: int
    counts: list[CountOut]
