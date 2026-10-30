"""The protocol of the test fetch between the API and the worker (ADR-0013).

An administrator can ask for one trial fetch of a source before enabling it. The API sends the
worker the identifiers of the source and of the test, nothing else; the worker fetches through the
same connector and `SafeHttpClient` as a scheduled fetch, writes nothing to the database and leaves
a small answer in Redis under its own prefix; the API reads that answer **as untrusted data**.

This module has no network in it and is imported by both sides. It holds the names of the keys, the
limits, the shape of the answer and the strict reader the API uses: the worker handles hostile
content, so whatever it writes is checked again before anyone sees it.
"""

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pharos.ingest.links import safe_link
from pharos.ingest.text import clean_text

TASK_NAME = "pharos.ingest.test_fetch"
# The worker's Redis user owns this prefix; the API's markers and cooldowns live under another one
# that the worker's user cannot touch.
RESULT_PREFIX = "pharos:worker:testfetch:"
MARKER_PREFIX = "pharos:api:testfetch:"
COOLDOWN_PREFIX = "pharos:api:testfetch-cooldown:"
RATE_PREFIX = "pharos:api:testfetch-rate:"

COOLDOWN_SECONDS = 30
ORG_TESTS_PER_MINUTE = 10  # all sources and all administrators of an organization together
RATE_WINDOW_SECONDS = 60
MARKER_TTL_SECONDS = 300
RESULT_TTL_SECONDS = 120
TASK_EXPIRES_SECONDS = 60

MAX_SAMPLES = 5
MAX_RAW_BYTES = 16 * 1024  # an answer is a few hundred bytes; more than this is not one
MAX_TITLE = 300
MAX_URL = 2048
_COUNT_LIMIT = 10_000_000

# The words an answer may use: the outcomes of a fetch (see connectors.base.OUTCOMES), plus the two
# ways a test can end before a fetch happens.
Outcome = Literal[
    "ok",
    "blocked",
    "timeout",
    "too_large",
    "bad_status",
    "bad_content",
    "network",
    "error",
    "host_busy",
    "source_missing",
]
_CODE = re.compile(r"[a-z_]{1,64}")


def result_key(test_id: uuid.UUID) -> str:
    return f"{RESULT_PREFIX}{test_id}"


def marker_key(test_id: uuid.UUID) -> str:
    return f"{MARKER_PREFIX}{test_id}"


def cooldown_key(org_id: uuid.UUID, source_id: uuid.UUID) -> str:
    return f"{COOLDOWN_PREFIX}{org_id}:{source_id}"


def rate_key(org_id: uuid.UUID) -> str:
    return f"{RATE_PREFIX}{org_id}"


class Sample(BaseModel):
    """One article the trial fetch found: enough to see that the source works, nothing more."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=MAX_TITLE)
    url: str = Field(max_length=MAX_URL)
    published_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def _plain_text(cls, value: str) -> str:
        # What the worker sends must be what cleaning leaves: the reader does not repair text, it
        # refuses text that is not plain (and so what is shown is what a real fetch would store).
        if not value or clean_text(value, MAX_TITLE) != value:
            raise ValueError("not plain text")
        return value

    @field_validator("url")
    @classmethod
    def _plain_link(cls, value: str) -> str:
        link = safe_link(value)
        if link is None:
            raise ValueError("not a plain web link")
        return link


class TestResult(BaseModel):
    """The worker's answer. Every field is bounded and every word comes from a fixed list."""

    __test__ = False  # not a test class, whatever pytest thinks of the name

    model_config = ConfigDict(extra="forbid")

    v: Literal[1]
    test_id: uuid.UUID
    source_id: uuid.UUID
    outcome: Outcome
    error_code: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    entries_seen: int = Field(default=0, ge=0, le=_COUNT_LIMIT)
    entries_skipped: int = Field(default=0, ge=0, le=_COUNT_LIMIT)
    bytes_fetched: int = Field(default=0, ge=0, le=_COUNT_LIMIT)
    sample: list[Sample] = Field(default_factory=list, max_length=MAX_SAMPLES)

    @field_validator("error_code")
    @classmethod
    def _fixed_word(cls, value: str | None) -> str | None:
        if value is not None and not _CODE.fullmatch(value):
            raise ValueError("not a fixed word")
        return value


def encode(result: TestResult) -> str:
    return result.model_dump_json()


def parse_result(
    raw: bytes | str | None, *, test_id: uuid.UUID, source_id: uuid.UUID
) -> TestResult | None:
    """The worker's answer if, and only if, it is exactly what the protocol allows and belongs to
    this test of this source. None otherwise (the caller reports it as unavailable)."""
    if raw is None:
        return None
    size = len(raw) if isinstance(raw, bytes) else len(raw.encode("utf-8", "replace"))
    if size > MAX_RAW_BYTES:
        return None
    try:
        # Strict: "200" is not a number, true is not 1, 1.5 is not an integer. The worker writes
        # exact types, so anything looser than that is not the worker's.
        result = TestResult.model_validate_json(raw, strict=True)
    except (ValueError, RecursionError, ValidationError):
        return None
    if result.test_id != test_id or result.source_id != source_id:
        return None
    return result
