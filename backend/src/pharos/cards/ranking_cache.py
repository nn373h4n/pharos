"""The shared part of the Home feed's ranking, kept in Redis for a few minutes (ADR-0027, F-49).

One feed request used to rank every article of the window for the person who asked. Most of that
work is the same for everybody with the same profession at the same moment, so it is done once for
each (organization, profession, five minutes) and kept: the best `RANKING_SIZE` articles by the
part of the score that does not depend on the person, with each article's category. Nothing about
a person is in it. The caller adds the person's own part, and the list stays exact as far as the
certificate in `exact_from` says; beyond that the live query takes over.

Redis is only a cache. Whatever goes wrong with it (down, slow, a value that does not parse) the
caller gets None and answers with the live query, which is correct and slow.
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from redis.asyncio import Redis
from redis.exceptions import RedisError

from pharos.cards.ranking import score_ceiling
from pharos.cards.repository import Candidate

logger = logging.getLogger(__name__)

RANKING_SIZE = 2000
TTL_SECONDS = 900  # three rankings' worth: the five minutes it was made for and the pages after
LOCK_SECONDS = 20
WAIT_SECONDS = 3.0
POLL_SECONDS = 0.05
KEY_PREFIX = "pharos:api:feed:v1"
_PROFESSION = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


@dataclass(frozen=True, slots=True)
class Ranking:
    """The shared ranking of one profession at one moment."""

    candidates: list[Candidate]
    # Fewer candidates than were asked for: the list holds every article there is.
    complete: bool

    def exact_from(self) -> int | None:
        """Scores above this are certain to be in the list and in order; None means all of them.

        An article that is not in the list has a shared part no higher than the last one's, so
        its whole score cannot be higher than that plus the most a person's own part adds."""
        if self.complete or not self.candidates:
            return None
        return score_ceiling(self.candidates[-1].base)


def key_for(org: uuid.UUID, profession: str, moment: datetime) -> str | None:
    """The key of one ranking, or None when the profession code is not one we would put in a key."""
    if not _PROFESSION.fullmatch(profession):
        return None
    return f"{KEY_PREFIX}:{org}:{profession}:{int(moment.timestamp())}"


def _encode(ranking: Ranking) -> str:
    return json.dumps(
        {
            "c": ranking.complete,
            "e": [[str(c.id), c.category, str(c.base)] for c in ranking.candidates],
        },
        separators=(",", ":"),
    )


def _decode(raw: str | bytes) -> Ranking | None:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("c"), bool):
            return None
        entries = data["e"]
        if not isinstance(entries, list) or len(entries) > RANKING_SIZE:
            return None
        found: list[Candidate] = []
        for item in entries:
            if not (isinstance(item, list) and len(item) == 3):
                return None
            ident, category, base = item
            if not (isinstance(category, str) and isinstance(base, str)):
                return None
            found.append(Candidate(id=uuid.UUID(str(ident)), category=category, base=Decimal(base)))
        return Ranking(candidates=found, complete=data["c"])
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return None


class RankingCache:
    def __init__(
        self,
        redis: Redis,
        *,
        size: int = RANKING_SIZE,
        ttl_seconds: int = TTL_SECONDS,
        wait_seconds: float = WAIT_SECONDS,
    ) -> None:
        self._redis = redis
        self._size = size
        self._ttl = ttl_seconds
        self._wait = wait_seconds

    @property
    def size(self) -> int:
        return self._size

    async def get_or_rank(
        self,
        key: str,
        rank: Callable[[int], Awaitable[list[Candidate]]],
        *,
        make: bool = True,
    ) -> Ranking | None:
        """The ranking under `key`; made by the first caller while the others wait for it.

        `rank(limit)` does the work against the database. With `make` false a ranking is only
        read, never made (for a period that is over). None means: do not use the cache, rank
        with the live query."""
        try:
            found = await self._read(key)
            if found is not None:
                return found
            if not make:
                return None
            lock = f"{key}:lock"
            token = uuid.uuid4().hex
            if await self._redis.set(lock, token, nx=True, ex=LOCK_SECONDS):
                try:
                    return await self._make(key, rank)
                finally:
                    # Only the one that took the lock lets it go: after LOCK_SECONDS it may be
                    # somebody else's.
                    if await self._redis.get(lock) in (token, token.encode()):
                        await self._redis.delete(lock)
            # Another request is making it: wait for it a little. If it is not there by then, this
            # request answers with the live query for its own page: making the whole ranking again
            # would add the most work when the database is busiest.
            waited = 0.0
            while waited < self._wait:
                await asyncio.sleep(POLL_SECONDS)
                waited += POLL_SECONDS
                found = await self._read(key)
                if found is not None:
                    return found
            return None
        except (RedisError, OSError, TimeoutError):
            logger.warning("feed ranking cache unavailable; ranking without it")
            return None

    async def _read(self, key: str) -> Ranking | None:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        ranking = _decode(raw)
        if ranking is None:
            logger.warning("feed ranking cache: an unreadable value was ignored")
        return ranking

    async def _make(self, key: str, rank: Callable[[int], Awaitable[list[Candidate]]]) -> Ranking:
        candidates = await rank(self._size)
        ranking = Ranking(candidates=candidates, complete=len(candidates) < self._size)
        await self._redis.set(key, _encode(ranking), ex=self._ttl)
        return ranking
