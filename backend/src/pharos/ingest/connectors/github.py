"""GitHub releases (ADR-0006).

Configuration: `{"repo": "owner/name"}`. One request per run to
`https://api.github.com/repos/<owner>/<name>/releases`; the release pages themselves are never
fetched. Drafts and pre-releases are skipped (a beta every week is noise for a reader).

The token (`PHAROS_GITHUB_TOKEN`, fine-grained, no scopes) is optional and only raises the rate
limit. It is handed to the connector when it is built, sent as a `Bearer` that `SafeHttpClient`
binds to `api.github.com` over HTTPS (never on a redirect to another host), and never appears in a
URL, an error, a log line or the stored configuration of a source.
"""

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pharos.ingest.connectors.base import (
    ConfigError,
    ConnectorError,
    FetchOutcome,
    FetchState,
    error_from_fetch,
)
from pharos.ingest.connectors.common import DATE_LIMIT, entry, load_json, moment, validator
from pharos.ingest.feed import MAX_ENTRIES
from pharos.net.safe_http import Bearer, FetchError, SafeHttpClient

HOST = "api.github.com"
PER_PAGE = 30
# GitHub's own rules: an owner is letters, digits and single hyphens (at most 39); a repository
# name is letters, digits, `.`, `_` and `-` (at most 100).
_REPO = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})/([A-Za-z0-9._-]{1,100})")


class GithubConnector:
    type = "github"

    def __init__(self, token: str | None = None) -> None:
        self._token = token or None

    def __repr__(self) -> str:  # the token must not show up in a log line or a traceback
        return f"GithubConnector(token={'set' if self._token else 'none'})"

    def validate_config(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if set(raw) != {"repo"}:
            raise ConfigError("unknown_setting" if "repo" in raw else "repo_required")
        repo = raw["repo"]
        match = _REPO.fullmatch(repo) if isinstance(repo, str) else None
        if match is None or match.group(2) in (".", "..") or match.group(2).endswith(".git"):
            raise ConfigError("repo_invalid")
        return {"repo": repo}

    def dedupe_key(self, config: Mapping[str, Any]) -> str:
        return hashlib.sha256(f"github:{str(config['repo']).lower()}".encode()).hexdigest()

    def politeness_host(self, config: Mapping[str, Any]) -> str | None:
        return HOST

    async def fetch(
        self,
        config: Mapping[str, Any],
        state: FetchState,
        http: SafeHttpClient,
        now: datetime,
    ) -> FetchOutcome:
        try:  # the stored configuration is re-checked with every rule, never trusted
            repo = self.validate_config(config)["repo"]
        except ConfigError:
            raise ConnectorError("error", "config_invalid") from None
        headers = {"Accept": "application/vnd.github+json"}
        if etag_out := validator(state.etag):
            headers["If-None-Match"] = etag_out
        if modified_out := validator(state.last_modified, DATE_LIMIT):
            headers["If-Modified-Since"] = modified_out
        bearer = Bearer(HOST, self._token) if self._token else None
        try:
            result = await http.fetch(
                f"https://{HOST}/repos/{repo}/releases?per_page={PER_PAGE}",
                headers=headers,
                bearer=bearer,
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
        releases = load_json(result.body)
        if not isinstance(releases, list):
            raise ConnectorError("bad_content", "json_shape")

        entries = []
        skipped = 0
        for release in releases[:MAX_ENTRIES]:
            made = _release(repo, release, now)
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
            entries_seen=min(len(releases), MAX_ENTRIES),
            entries_skipped=skipped,
        )


def _release(repo: str, release: object, now: datetime):  # type: ignore[no-untyped-def]
    if not isinstance(release, dict) or release.get("draft") is True:
        return None
    if release.get("prerelease") is True:
        return None
    label = release.get("name") or release.get("tag_name")
    if not isinstance(label, str) or not label.strip():
        return None
    identifier = release.get("id")
    return entry(
        str(identifier)
        if isinstance(identifier, int) and not isinstance(identifier, bool)
        else None,
        f"{repo}: {label}",
        release.get("body"),
        release.get("html_url"),
        moment(release.get("published_at"), now),
    )
