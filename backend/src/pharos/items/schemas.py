"""Request and response bodies of the item endpoints."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

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
    sources: list[SourceRef]


class ItemListOut(BaseModel):
    items: list[ItemOut]
    total: int
