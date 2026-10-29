"""From stored entries to items: canonical link, exact de-duplication, hold flags (M3-08).

Runs in the transaction that stores the raw entries, so a lost lease discards both together. Only
entries that are new or changed are looked at (at most the parser's cap of 200), and everything is
written with a few statements for the whole batch.

* One article is one item: the canonical link (tracking parameters, fragment, case of the host and
  the default port removed; every other parameter kept, Hacker News needs `?id=`) is hashed, and
  `(org_id, owner_user_id, url_hash)` is unique. An article seen in several sources is one item with
  several `item_sources`. An item from a personal feed has an owner and is never merged into an
  organization item, so what somebody follows cannot leak into shared items.
* An entry whose content did not change is a no-op: after the raw copy was cleaned up and stored
  again, the item is left as it is.
* Hold rules only raise a flag for a person to look at (`status = held`, a short `hold_reason`).
  They
  do not decide what is true or safe; text from a feed stays untrusted data for every later step.
  A person's decision (`hidden`, `held`, approved) is never changed here: the worker has no grant
  to update `status` or `hold_reason`.
"""

import hashlib
import ipaddress
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from pharos.ingest.feed import RawEntry
from pharos.ingest.links import safe_link

_TRACKING_PARAMETERS = frozenset({"fbclid", "gclid"})
_TRACKING_PREFIX = "utm_"
_DEFAULT_PORTS = {"http": 80, "https": 443}
# Browsers read a host whose last label is a number (decimal, octal or 0x hex) as an IPv4 address:
# 2130706433, 127.1 and 0x7f000001 all open 127.0.0.1.
_NUMERIC_LABEL = re.compile(r"0x[0-9a-f]*|[0-9]+", re.IGNORECASE)

# Phrases that address a model instead of a reader. A flag for a person to check, not a verdict:
# the list is short on purpose, because every false alarm hides a good article from the feed.
_INSTRUCTION_PHRASES = re.compile(
    r"(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|everything\s+)?(?:of\s+)?(?:the\s+)?"
    r"(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|messages?)"
    r"|(?:reveal|print|repeat|show)\s+(?:your\s+|the\s+)?system\s+prompt"
    r"|you\s+are\s+now\s+(?:in\s+)?(?:developer|dan|jailbreak)\b",
    re.IGNORECASE,
)

HOLD_IP_HOST = "link_ip_host"
HOLD_ODD_PORT = "link_odd_port"
HOLD_INSTRUCTION = "instruction_phrase"


@dataclass(frozen=True, slots=True)
class Candidate:
    """An entry ready to become an item."""

    canonical_url: str
    url_hash: str
    title: str
    excerpt: str
    published_at: datetime | None
    content_sha256: str
    hold_reason: str | None


def canonical_url(url: str) -> str | None:
    """The link without tracking noise, or None if it is not a plain http(s) address."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".")  # "example.com." is the same host
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    # IPv6 literals need their brackets back; hostname strips them (and lowercases the name).
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    canonical = urlunsplit((scheme, netloc, path, _query(parts.query), ""))
    # The result must be storable too: it is checked again, because dropping parameters can only
    # shorten a link, but the host and path rules above are not the feed parser's.
    return canonical if safe_link(canonical) == canonical else None


def _query(raw: str) -> str:
    """The query without tracking parameters. Nothing is decoded and encoded again: that would
    lengthen the link past the database limit, turn `?x` into `?x=`, and make different
    invalid escapes (`%FF`, `%FE`) the same."""
    kept = []
    for pair in raw.split("&"):
        name = unquote(pair.split("=", 1)[0]).lower()  # decoded only to compare
        if pair and not name.startswith(_TRACKING_PREFIX) and name not in _TRACKING_PARAMETERS:
            kept.append(pair)
    return "&".join(kept)


def hold_reason(title: str, excerpt: str, url: str) -> str | None:
    """Why a person should look at this entry before it is used further, or None."""
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
            return HOLD_IP_HOST
    else:
        return HOLD_IP_HOST
    # The canonical link has no default port left, so any port that is still there is unusual.
    if parts.port is not None:
        return HOLD_ODD_PORT
    if _INSTRUCTION_PHRASES.search(f"{title}\n{excerpt}"):
        return HOLD_INSTRUCTION
    return None


def candidates(entries: Sequence[RawEntry]) -> list[Candidate]:
    """One candidate per canonical link (the first entry wins); entries without a usable link are
    dropped."""
    seen: dict[str, Candidate] = {}
    for entry in entries:
        canonical = canonical_url(entry.url)
        if canonical is None or not entry.title:
            continue
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        if digest in seen:
            continue
        seen[digest] = Candidate(
            canonical,
            digest,
            entry.title,
            entry.excerpt,
            entry.published_at,
            entry.content_sha256,
            hold_reason(entry.title, entry.excerpt, canonical),
        )
    # In a fixed order, so two fetches that share links take the row locks in the same order.
    return sorted(seen.values(), key=lambda candidate: candidate.url_hash)


async def store_items(
    connection: AsyncConnection,
    org: uuid.UUID,
    source: uuid.UUID,
    owner: uuid.UUID | None,
    entries: Sequence[RawEntry],
) -> int:
    """Create or refresh the items for these entries and link them to the source. Returns how many
    items are new. Running it again with the same entries changes nothing and returns 0."""
    wanted = candidates(entries)
    if not wanted:
        return 0
    arrays = {
        "urls": [c.canonical_url for c in wanted],
        "hashes": [c.url_hash for c in wanted],
        "titles": [c.title for c in wanted],
        "excerpts": [c.excerpt for c in wanted],
        "published": [c.published_at for c in wanted],
        "contents": [c.content_sha256 for c in wanted],
        "holds": [c.hold_reason for c in wanted],
    }
    inserted = await connection.execute(
        text(
            "INSERT INTO items (org_id, owner_user_id, canonical_url, url_hash, title, excerpt, "
            "published_at, content_sha256, status, hold_reason) "
            "SELECT :org, :owner, i.url, i.hash, i.title, i.excerpt, i.published, i.content, "
            "CASE WHEN i.hold IS NULL THEN 'new' ELSE 'held' END, i.hold "
            "FROM unnest(CAST(:urls AS text[]), CAST(:hashes AS text[]), CAST(:titles AS text[]), "
            "CAST(:excerpts AS text[]), CAST(:published AS timestamptz[]), "
            "CAST(:contents AS text[]), CAST(:holds AS text[])) "
            "AS i(url, hash, title, excerpt, published, content, hold) "
            "ON CONFLICT (org_id, owner_user_id, url_hash) DO UPDATE SET "
            "title = EXCLUDED.title, excerpt = EXCLUDED.excerpt, "
            "published_at = EXCLUDED.published_at, content_sha256 = EXCLUDED.content_sha256 "
            "WHERE items.content_sha256 <> EXCLUDED.content_sha256 "
            # Text a rule flags never replaces the text of an item that exists: a clean, perhaps
            # approved, item cannot be turned into a flagged one without a person seeing it.
            "AND EXCLUDED.status = 'new' "
            # Only the source that first reported the article may edit its text; another source
            # reporting the same link adds a link, not a new title.
            "AND (SELECT s.source_id FROM item_sources s WHERE s.org_id = items.org_id "
            "AND s.item_id = items.id ORDER BY s.first_seen_at, s.source_id LIMIT 1) = :source "
            "RETURNING (xmax = 0) AS created"
        ),
        {**arrays, "org": org, "owner": owner, "source": source},
    )
    created = sum(1 for row in inserted.all() if row.created)
    # Two statements instead of IS NOT DISTINCT FROM, which cannot use the unique index.
    same_owner = "t.owner_user_id IS NULL" if owner is None else "t.owner_user_id = :owner"
    await connection.execute(
        text(
            "INSERT INTO item_sources (org_id, item_id, source_id) "  # noqa: S608 (same_owner is one of two constant fragments)
            "SELECT :org, t.id, :source FROM items t "
            f"WHERE t.org_id = :org AND {same_owner} "
            "AND t.url_hash = ANY(CAST(:hashes AS text[])) "
            "ON CONFLICT DO NOTHING"
        ),
        {"org": org, "source": source, "hashes": arrays["hashes"]}
        | ({"owner": owner} if owner is not None else {}),
    )
    return created
