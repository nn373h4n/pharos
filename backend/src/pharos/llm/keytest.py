"""The protocol of "test the key" between the API and the worker (ADR-0013, ADR-0015 point 7).

An administrator can ask whether the model key in the worker's environment works. The API never
holds the key: it sends the worker the identifier of the test, nothing else; the worker makes one
tiny call through the gateway (booked against the budget like any other) and leaves a small answer
in Redis under its own prefix; the API reads that answer as untrusted data with a strict reader.

This module has no network in it and is imported by both sides.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

TASK_NAME = "pharos.llm.test_key"
# The worker's Redis user owns the first two prefixes; the API's markers and cooldowns live under
# a prefix the worker's user cannot touch.
RESULT_PREFIX = "pharos:worker:llm:keytest:"
LAST_PREFIX = "pharos:worker:llm:keytest-last:"
MARKER_PREFIX = "pharos:api:llm-keytest:"
COOLDOWN_PREFIX = "pharos:api:llm-keytest-cooldown:"

COOLDOWN_SECONDS = 60
MARKER_TTL_SECONDS = 300
RESULT_TTL_SECONDS = 300
LAST_TTL_SECONDS = 30 * 24 * 3600
TASK_EXPIRES_SECONDS = 90
MAX_RAW_BYTES = 1024

# What a test can end with. "ok": the provider answered with this key. The rest say why not.
Outcome = Literal[
    "ok",
    "disabled",  # no provider is named in the settings
    "not_configured",  # a provider, but no key or no model for the check
    "rejected",  # the provider refused the key or the request
    "failed",  # no answer in time, or an error that is worth another try
    "budget",  # the day's cap is reached
    "paced",  # too many calls this minute
    "paused",  # the stage keeps failing and is resting
]


class KeyTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    test_id: uuid.UUID
    outcome: Outcome
    checked_at: datetime


def result_key(test_id: uuid.UUID) -> str:
    return f"{RESULT_PREFIX}{test_id}"


def last_key(org_id: uuid.UUID) -> str:
    return f"{LAST_PREFIX}{org_id}"


def marker_key(test_id: uuid.UUID) -> str:
    return f"{MARKER_PREFIX}{test_id}"


def cooldown_key(org_id: uuid.UUID) -> str:
    return f"{COOLDOWN_PREFIX}{org_id}"


def encode(result: KeyTestResult) -> str:
    return result.model_dump_json()


def parse_result(raw: bytes | str | None, *, test_id: uuid.UUID | None) -> KeyTestResult | None:
    """The worker's answer if, and only if, it is exactly what the protocol allows (and, when a
    test id is given, belongs to that test). None otherwise: the caller reports it as
    unavailable."""
    if raw is None:
        return None
    size = len(raw) if isinstance(raw, bytes) else len(raw.encode("utf-8", "replace"))
    if size > MAX_RAW_BYTES:
        return None
    try:
        result = KeyTestResult.model_validate_json(raw, strict=True)
    except (ValueError, RecursionError, ValidationError):
        return None
    if test_id is not None and result.test_id != test_id:
        return None
    return result
