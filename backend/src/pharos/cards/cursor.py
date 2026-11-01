"""The cursor of the card feed: opaque to the caller, strict for us.

It holds the moment the feed was ranked at (`as_of`), and the score and id of the last card of the
page. It carries nothing secret and only decides where the caller's own list continues, so it is
not signed; it is parsed strictly and good for one hour, after which the feed starts again.
"""

import base64
import binascii
import json
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from pharos.cards.ranking import CURSOR_LIFETIME_SECONDS, SCORE_SCALE


class Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    a: datetime  # as_of
    s: StrictInt = Field(ge=0, le=SCORE_SCALE)  # score of the last card
    i: uuid.UUID  # id of the last card


def encode(as_of: datetime, score: int, item: uuid.UUID) -> str:
    body = json.dumps(
        {"a": as_of.astimezone(UTC).isoformat(), "s": score, "i": str(item)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")


def decode(raw: str, *, now: datetime) -> Cursor | None:
    """The cursor, or None when it is malformed, from the future or older than an hour."""
    if not raw or len(raw) > 200:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        cursor = Cursor.model_validate(json.loads(base64.urlsafe_b64decode(padded)))
    except (ValueError, binascii.Error, ValidationError, UnicodeDecodeError):
        return None
    if cursor.a.tzinfo is None or cursor.s < 0:
        return None
    age = (now - cursor.a).total_seconds()
    return cursor if 0 <= age <= CURSOR_LIFETIME_SECONDS else None
