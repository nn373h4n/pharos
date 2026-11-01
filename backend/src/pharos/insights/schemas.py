"""Response bodies of the insight endpoint. Every text is plain text."""

from typing import Literal

from pydantic import BaseModel


class InsightBody(BaseModel):
    profession: str
    language: str
    summary: str
    how_to_apply: list[str]
    try_prompt: str
    safety_note: str


class InsightOut(BaseModel):
    """`ready` carries the insight; `pending` means the worker has been asked and the page should
    ask again in a few seconds; `unavailable` says why there will be none for now."""

    status: Literal["ready", "pending", "unavailable"]
    reason: Literal["off", "rejected", "invalid"] | None
    insight: InsightBody | None
