"""Request and response bodies of the item endpoints."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

ItemStatus = Literal["new", "held", "hidden"]


class SourceRef(BaseModel):
    id: uuid.UUID
    name: str


class ItemOut(BaseModel):
    """One article. The text is plain text and `url` an `http(s)` address; the page behind it was
    never fetched. `sources` and `hold_reason` are for administrators and editors."""

    id: uuid.UUID
    url: str
    title: str
    excerpt: str
    lang: str | None
    published_at: datetime | None
    fetched_at: datetime
    status: ItemStatus
    hold_reason: str | None
    updated_at: datetime | None  # reviewers only
    summary: str | None  # what the model said; plain text, empty until the article is read
    category: str | None
    quality: int | None  # 1 to 5, reviewers only
    sources: list[SourceRef]


class ReviewIn(BaseModel):
    """The optional body of a review step (F-26). `if_updated_at` is the `updated_at` of the
    article as the reviewer saw it: when the article has changed since (its text, what the model
    said about it, or its state), the step is refused with `409 item_changed` instead of applying
    to something the reviewer did not see. Without it the step applies to the article as it is."""

    model_config = ConfigDict(extra="forbid")

    if_updated_at: AwareDatetime | None = None


class ItemListOut(BaseModel):
    items: list[ItemOut]
    total: int
