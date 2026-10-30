"""What a source's configuration may say, and when two sources are the same one (ADR-0006).

This module has no network in it: it imports no HTTP client, and nothing that does. The connectors
use it to check the configuration they were given, and the API uses it to check what an
administrator typed, so the API never has to load the code that fetches (an import-linter contract
keeps it so).

Which hosts are acceptable is decided twice. Here an obviously internal address is refused early,
with a clear code, so the administrator learns about it when saving. The real check is made by
`SafeHttpClient` when the connection is opened, after the name has been resolved, and does not
depend on this one.
"""

import hashlib
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pharos.ingest.links import safe_link
from pharos.ingest.text import clean_text

SOURCE_TYPES = ("rss", "github", "hackernews")

# GitHub's own rules: an owner is letters, digits and single hyphens (at most 39); a repository
# name is letters, digits, `.`, `_` and `-` (at most 100).
GITHUB_REPO = re.compile(
    r"([A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})/([A-Za-z0-9._-]{1,100})"
)
MAX_QUERY = 100
MAX_POINTS = 10_000
DEFAULT_POINTS = 20

_DEFAULT_PORTS = {"http": 80, "https": 443}
# Browsers read a host whose last label is a number (decimal, octal or 0x hex) as an IPv4 address:
# 2130706433, 127.1 and 0x7f000001 all open 127.0.0.1.
_NUMERIC_LABEL = re.compile(r"0x[0-9a-f]*|[0-9]+", re.IGNORECASE)
# Names that only make sense inside a network. A name without a dot is a machine on the local
# network (in Docker: `redis`, `postgres`, `api`).
_INTERNAL_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".lan", ".home.arpa")


class ConfigError(ValueError):
    """The configuration of a source is not acceptable. `code` is a fixed word, not user text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Validated:
    config: dict[str, Any]
    dedupe_key: str


def validate(
    source_type: str, raw: Mapping[str, Any], *, blocked_hosts: Collection[str] = ()
) -> Validated:
    """The normalized configuration and the key of a source, or ConfigError.

    `blocked_hosts` are names that must not be fetched even though they look ordinary (the host of
    this installation's own address)."""
    if source_type == "rss":
        config = validate_rss(raw)
        refuse_internal_host(config["url"], blocked_hosts)
        return Validated(config, rss_key(config))
    if source_type == "github":
        config = validate_github(raw)
        return Validated(config, github_key(config))
    if source_type == "hackernews":
        config = validate_hackernews(raw)
        return Validated(config, hackernews_key(config))
    raise ConfigError("unknown_type")


# --- RSS and Atom ---------------------------------------------------------------------------------


def validate_rss(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != {"url"}:
        raise ConfigError("unknown_setting" if "url" in raw else "url_required")
    url = safe_link(raw["url"])
    if url is None:
        raise ConfigError("url_invalid")
    return {"url": url}


def ascii_host(name: str) -> str | None:
    """The host as a connection will see it: lower case, ASCII (IDNA), no dot at the end. None if it
    cannot be written that way. The IDNA codec reads the ideographic full stops (U+3002, U+FF0E,
    U+FF61) as dots, as an HTTP client does, so an address followed by one is the same machine as
    without it, and the rules below must see that."""
    if ":" in name:  # an IPv6 address: it has no dots to fold and no letters to convert
        return name.lower()
    try:
        folded = name.lower().encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return folded.rstrip(".")


def normalized_feed_url(url: str) -> str:
    """One spelling of the address: lower-case scheme and host, no default port, no fragment."""
    parts = urlsplit(url)
    raw = parts.hostname or ""
    host = ascii_host(raw)
    host = raw.lower().rstrip(".") if host is None else host
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    netloc = host if port in (None, _DEFAULT_PORTS[parts.scheme.lower()]) else f"{host}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def rss_key(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(f"rss:{normalized_feed_url(config['url'])}".encode()).hexdigest()


def refuse_internal_host(url: str, blocked_hosts: Collection[str] = ()) -> None:
    """Refuse an address that can only be an internal machine: an IP address in any spelling a
    browser accepts, a name without a dot (`localhost`, `redis`), a name that ends in an internal
    suffix, or one of `blocked_hosts`. Raises ConfigError("url_not_allowed")."""
    host = ascii_host(urlsplit(url).hostname or "")
    if not host or host in {ascii_host(h) for h in blocked_hosts}:  # also: not writable as ASCII
        raise ConfigError("url_not_allowed")
    # An IPv4 address in any spelling ends in a number (127.0.0.1, 127.1, 2130706433, 0x7f000001),
    # and an IPv6 address has no dot unless it ends in an IPv4 one, so these two rules cover both.
    if _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
        raise ConfigError("url_not_allowed")
    if "." not in host or host.endswith(_INTERNAL_SUFFIXES):
        raise ConfigError("url_not_allowed")


# --- GitHub releases ------------------------------------------------------------------------------


def validate_github(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != {"repo"}:
        raise ConfigError("unknown_setting" if "repo" in raw else "repo_required")
    repo = raw["repo"]
    match = GITHUB_REPO.fullmatch(repo) if isinstance(repo, str) else None
    if match is None or match.group(2) in (".", "..") or match.group(2).endswith(".git"):
        raise ConfigError("repo_invalid")
    return {"repo": repo}


def github_key(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(f"github:{str(config['repo']).lower()}".encode()).hexdigest()


# --- Hacker News ----------------------------------------------------------------------------------


def validate_hackernews(raw: Mapping[str, Any]) -> dict[str, Any]:
    if "query" not in raw:
        raise ConfigError("query_required")
    if not set(raw) <= {"query", "min_points"}:
        raise ConfigError("unknown_setting")
    query = raw["query"]
    # Plain words only: what is searched is cleaned like any other text, and a query that changes
    # when cleaned (markup, control or invisible characters) is refused, not repaired.
    if not isinstance(query, str) or clean_text(query, MAX_QUERY + 1) != query.strip():
        raise ConfigError("query_invalid")
    query = query.strip()
    if not 1 <= len(query) <= MAX_QUERY:
        raise ConfigError("query_invalid")
    points = raw.get("min_points", DEFAULT_POINTS)
    if not isinstance(points, int) or isinstance(points, bool) or not 0 <= points <= MAX_POINTS:
        raise ConfigError("min_points_invalid")
    return {"query": query, "min_points": points}


def hackernews_key(config: Mapping[str, Any]) -> str:
    text = f"hackernews:{str(config['query']).lower()}:{config['min_points']}"
    return hashlib.sha256(text.encode()).hexdigest()
