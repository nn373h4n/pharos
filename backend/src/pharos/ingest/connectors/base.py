"""What every connector is, and what it can hand back (ADR-0006).

A connector knows one kind of source. It receives its validated configuration, the validators from
the last fetch, and a `SafeHttpClient`; it has no database, no settings and no other way out to the
network (an import-linter contract forbids it from importing an HTTP library). It returns cleaned
entries or raises `ConnectorError` with a coarse outcome, so the caller never sees raw error text.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from pharos.ingest.feed import RawEntry
from pharos.net.safe_http import FetchError, FetchErrorKind, SafeHttpClient

# The outcomes a fetch run can end with (fetch_runs.outcome), apart from "ok" and "not_modified".
OUTCOMES = frozenset(
    {"blocked", "timeout", "too_large", "bad_status", "bad_content", "network", "error"}
)
_STATUS = re.compile(r"^status_(\d{3})$")


class ConfigError(ValueError):
    """The configuration of a source is not acceptable. `code` is a fixed word, not user text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ConnectorError(Exception):
    """A fetch failed. Everything here is safe to store and show: no URL, no address, no message."""

    def __init__(
        self,
        outcome: str,
        code: str,
        *,
        http_status: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        if outcome not in OUTCOMES or not re.fullmatch(r"[a-z_]{1,64}", code):
            outcome, code = "error", "unexpected"
        super().__init__(f"{outcome}:{code}")  # only after the check: str(error) is logged
        self.outcome = outcome
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after


def error_from_fetch(error: FetchError) -> ConnectorError:
    """The fixed words for a failed fetch; the reason text may carry addresses and is dropped."""
    status = _STATUS.match(error.reason)
    if error.kind is FetchErrorKind.BAD_STATUS and status:
        return ConnectorError(
            "bad_status",
            "http_error",
            http_status=int(status.group(1)),
            retry_after=error.retry_after,
        )
    codes = {
        FetchErrorKind.BLOCKED: "not_allowed",
        FetchErrorKind.TIMEOUT: "timed_out",
        FetchErrorKind.TOO_LARGE: "too_large",
        FetchErrorKind.BAD_STATUS: "redirects",
        FetchErrorKind.BAD_CONTENT: "unreadable",
        FetchErrorKind.NETWORK: "unreachable",
    }
    return ConnectorError(
        error.kind.value, codes.get(error.kind, "unexpected"), retry_after=error.retry_after
    )


@dataclass(frozen=True, slots=True)
class FetchState:
    """What the last successful fetch told us, to ask "anything new?" next time."""

    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    kind: Literal["ok", "not_modified"]
    entries: tuple[RawEntry, ...] = ()
    etag: str | None = None
    last_modified: str | None = None
    bytes_fetched: int = 0
    entries_seen: int = 0
    entries_skipped: int = 0


class Connector(Protocol):
    type: str

    def validate_config(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """The normalized configuration, or ConfigError."""

    def dedupe_key(self, config: Mapping[str, Any]) -> str:
        """Two sources with the same key are the same source."""

    async def fetch(
        self,
        config: Mapping[str, Any],
        state: FetchState,
        http: SafeHttpClient,
        now: datetime,
    ) -> FetchOutcome:
        """One fetch. Raises ConnectorError; never returns partial or unchecked data."""
