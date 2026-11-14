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
    look again in a few seconds; `absent` (only from GET) means nobody has asked, or the ask ran
    out without an answer, so the page asks with POST; `unavailable` says why there will be none
    for now."""

    status: Literal["ready", "pending", "absent", "unavailable"]
    reason: Literal["off", "rejected", "invalid"] | None
    insight: InsightBody | None
