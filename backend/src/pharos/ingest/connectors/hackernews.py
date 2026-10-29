"""Hacker News through the Algolia search API (ADR-0006).

Configuration: `{"query": "sap", "min_points": 20}`. One request per run to
`https://hn.algolia.com/api/v1/search_by_date` for stories matching the query with at least that
many points. Not the Firebase API, which would need one request per story. The linked articles are
never fetched; a story without a link (Ask HN) gets the address of its discussion page.
"""

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from pharos.ingest.connectors.base import (
    ConfigError,
    ConnectorError,
    FetchOutcome,
    FetchState,
    error_from_fetch,
)
from pharos.ingest.connectors.common import DATE_LIMIT, entry, load_json, moment, validator
from pharos.ingest.feed import MAX_ENTRIES
from pharos.ingest.links import safe_link
from pharos.ingest.text import clean_text
from pharos.net.safe_http import FetchError, SafeHttpClient

HOST = "hn.algolia.com"
HITS = 30
MAX_QUERY = 100
MAX_POINTS = 10_000
DEFAULT_POINTS = 20
_OBJECT_ID = re.compile(r"[0-9]{1,12}")


class HackerNewsConnector:
    type = "hackernews"

    def validate_config(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if "query" not in raw:
            raise ConfigError("query_required")
        if not set(raw) <= {"query", "min_points"}:
            raise ConfigError("unknown_setting")
        query = raw["query"]
        # Plain words only: what is searched is cleaned like any other text, and a query that
        # changes when cleaned (markup, control or invisible characters) is refused, not repaired.
        if not isinstance(query, str) or clean_text(query, MAX_QUERY + 1) != query.strip():
            raise ConfigError("query_invalid")
        query = query.strip()
        if not 1 <= len(query) <= MAX_QUERY:
            raise ConfigError("query_invalid")
        points = raw.get("min_points", DEFAULT_POINTS)
        if not isinstance(points, int) or isinstance(points, bool) or not 0 <= points <= MAX_POINTS:
            raise ConfigError("min_points_invalid")
        return {"query": query, "min_points": points}

    def dedupe_key(self, config: Mapping[str, Any]) -> str:
        text = f"hackernews:{str(config['query']).lower()}:{config['min_points']}"
        return hashlib.sha256(text.encode()).hexdigest()

    def politeness_host(self, config: Mapping[str, Any]) -> str | None:
        return HOST

    async def fetch(
        self,
        config: Mapping[str, Any],
        state: FetchState,
        http: SafeHttpClient,
        now: datetime,
    ) -> FetchOutcome:
        try:
            checked = self.validate_config(config)  # the stored configuration is re-checked
        except ConfigError:
            raise ConnectorError("error", "config_invalid") from None
        query = urlencode(
            {
                "query": checked["query"],
                "tags": "story",
                "numericFilters": f"points>={checked['min_points']}",
                "hitsPerPage": HITS,
            }
        )
        headers = {"Accept": "application/json"}
        if etag_out := validator(state.etag):
            headers["If-None-Match"] = etag_out
        if modified_out := validator(state.last_modified, DATE_LIMIT):
            headers["If-Modified-Since"] = modified_out
        try:
            result = await http.fetch(
                f"https://{HOST}/api/v1/search_by_date?{query}", headers=headers
            )
        except FetchError as error:
            raise error_from_fetch(error) from None

        etag = validator(result.headers.get("etag"))
        last_modified = validator(result.headers.get("last-modified"), DATE_LIMIT)
        if result.status == 304:
            return FetchOutcome(
                "not_modified",
                etag=etag or state.etag,
                last_modified=last_modified or state.last_modified,
            )
        document = load_json(result.body)
        hits = document.get("hits") if isinstance(document, dict) else None
        if not isinstance(hits, list):
            raise ConnectorError("bad_content", "json_shape")

        entries = []
        skipped = 0
        for hit in hits[:MAX_ENTRIES]:
            made = _story(hit, now)
            if made is None:
                skipped += 1
            else:
                entries.append(made)
        return FetchOutcome(
            "ok",
            entries=tuple(entries),
            etag=etag,
            last_modified=last_modified,
            bytes_fetched=len(result.body),
            entries_seen=min(len(hits), MAX_ENTRIES),
            entries_skipped=skipped,
        )


def _story(hit: object, now: datetime):  # type: ignore[no-untyped-def]
    if not isinstance(hit, dict):
        return None
    object_id = hit.get("objectID")
    if not isinstance(object_id, str) or not _OBJECT_ID.fullmatch(object_id):
        return None
    # The discussion page is the fallback, and also what a story links to when its own link is not
    # a plain http(s) address.
    link = safe_link(hit.get("url")) or f"https://news.ycombinator.com/item?id={object_id}"
    return entry(
        object_id,
        hit.get("title"),
        hit.get("story_text"),
        link,
        moment(hit.get("created_at_i"), now),
    )
