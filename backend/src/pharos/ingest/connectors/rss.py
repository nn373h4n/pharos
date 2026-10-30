"""RSS and Atom feeds (ADR-0006).

Configuration: `{"url": "https://example.org/feed.xml"}`. The page behind an entry's link is never
fetched; only the feed document itself is, through `SafeHttpClient`, with a conditional request when
the last fetch gave us validators.
"""

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from pharos.ingest.connectors.base import (
    ConnectorError,
    FetchOutcome,
    FetchState,
    error_from_fetch,
)
from pharos.ingest.feed import FeedError, parse_feed
from pharos.ingest.sourceconfig import normalized_feed_url, rss_key, validate_rss
from pharos.net.safe_http import FetchError, SafeHttpClient

__all__ = ["RssConnector", "normalized_feed_url"]

# What a server may give us to send back: visible ASCII only, and short (the database allows 512
# for an ETag and 128 for a date). Anything else is dropped rather than echoed into a request.
_VALIDATOR = re.compile(r"[\x20-\x7e]{1,512}")
_DATE_LIMIT = 128


def _validator(value: object, limit: int = 512) -> str | None:
    if isinstance(value, str) and len(value) <= limit and _VALIDATOR.fullmatch(value):
        return value
    return None


class RssConnector:
    type = "rss"

    def validate_config(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        return validate_rss(raw)

    def dedupe_key(self, config: Mapping[str, Any]) -> str:
        return rss_key(config)

    def politeness_host(self, config: Mapping[str, Any]) -> str | None:
        return (urlsplit(str(config.get("url", ""))).hostname or "").lower().rstrip(".") or None

    async def fetch(
        self,
        config: Mapping[str, Any],
        state: FetchState,
        http: SafeHttpClient,
        now: datetime,
    ) -> FetchOutcome:
        headers: dict[str, str] = {}
        if etag_out := _validator(state.etag):
            headers["If-None-Match"] = etag_out
        if modified_out := _validator(state.last_modified, _DATE_LIMIT):
            headers["If-Modified-Since"] = modified_out
        try:
            result = await http.fetch(config["url"], headers=headers)
        except FetchError as error:
            raise error_from_fetch(error) from None

        etag = _validator(result.headers.get("etag"))
        last_modified = _validator(result.headers.get("last-modified"), _DATE_LIMIT)
        if result.status == 304:
            # Keep what we had if the server did not repeat it.
            return FetchOutcome(
                "not_modified",
                etag=etag or state.etag,
                last_modified=last_modified or state.last_modified,
            )
        try:
            parsed = parse_feed(result.body, now)
        except FeedError as error:
            raise ConnectorError("bad_content", error.code) from None
        return FetchOutcome(
            "ok",
            entries=parsed.entries,
            etag=etag,
            last_modified=last_modified,
            bytes_fetched=len(result.body),
            entries_seen=parsed.total,
            entries_skipped=parsed.skipped,
        )
