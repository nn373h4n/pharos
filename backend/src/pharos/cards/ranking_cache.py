"""The shared part of the Home feed's ranking, kept in Redis for a few minutes (ADR-0027, F-49).

One feed request used to rank every article of the window for the person who asked. Most of that
work is the same for everybody with the same profession at the same moment, so it is done once for
each (organization, profession, five minutes) and kept: the best `RANKING_SIZE` articles by the
part of the score that does not depend on the person, with each article's category. Nothing about
a person is in it. The caller adds the person's own part, and the list stays exact as far as the
certificate in `exact_from` says; beyond that the live query takes over.

Redis is only a cache. Whatever goes wrong with it (down, slow, a value that does not parse) the
caller gets None and answers with the live query, which is correct and slow.

Each API process also keeps the rankings it has read, parsed, for as long as Redis keeps them
(F-50): reading and parsing the 150 KB list was most of a feed request's processor time. A ranking
carries a random mark that Redis keeps next to it, and a process uses its copy only while Redis
still holds the same mark (a 32-byte read), so a ranking deleted or made again in Redis is read
again; the copies are bounded by `LOCAL_ENTRIES` and by the Redis lifetime. Each kept ranking
also floors every article's shared part once; a person's whole score is then that plus whole
points for the bonuses, exactly what the live query computes, and a request only sorts 2,000 whole
numbers.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from redis.asyncio import Redis
from redis.exceptions import RedisError

from pharos.cards.ranking import INTEREST_POINTS, LEVEL_POINTS, scaled_base, score_ceiling
from pharos.cards.repository import Candidate

logger = logging.getLogger(__name__)

RANKING_SIZE = 2000
TTL_SECONDS = 900  # three rankings' worth: the five minutes it was made for and the pages after
LOCK_SECONDS = 20
# Short, because the waiting request holds a database connection (idle in its transaction) all the
# while: a ranking that takes longer is answered from the live query by the ones that gave up.
WAIT_SECONDS = 1.0
POLL_SECONDS = 0.05
KEY_PREFIX = "pharos:api:feed:v1"
_PROFESSION = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_STAMP = re.compile(r"[0-9a-f]{32}")
# Parsed rankings one API process keeps: two for each profession in use (this period and the one
# before), so 24 professions before one is read and parsed again; about 0.7 MB each once parsed.
LOCAL_ENTRIES = 48

Order = list[tuple[int, uuid.UUID]]
_LOWEST, _HIGHEST = Decimal(0), Decimal(1)


@dataclass(frozen=True, slots=True)
class Ranking:
    """The shared ranking of one profession at one moment."""

    candidates: list[Candidate]
    # Fewer candidates than were asked for: the list holds every article there is.
    complete: bool
    # A random mark of this making of the ranking, kept in Redis next to it (`<key>:n`). A process
    # uses its parsed copy only while Redis still holds the same mark, so a ranking deleted or
    # made again in Redis is read again (F-50). Not part of the ranking's value.
    stamp: str | None = field(default=None, compare=False)
    # Each article's shared part in whole points, worked out on first use and kept with the
    # ranking (F-50); not part of its value.
    _scaled: list[int] = field(default_factory=list, init=False, compare=False, repr=False)
    _ids: list[uuid.UUID] = field(default_factory=list, init=False, compare=False, repr=False)
    _categories: list[str] = field(default_factory=list, init=False, compare=False, repr=False)

    def exact_from(self) -> int | None:
        """Scores above this are certain to be in the list and in order; None means all of them.

        An article that is not in the list has a shared part no higher than the last one's, so
        its whole score cannot be higher than that plus the most a person's own part adds."""
        if self.complete or not self.candidates:
            return None
        return score_ceiling(self.candidates[-1].base)

    def order(self, interest_categories: frozenset[str], level_categories: frozenset[str]) -> Order:
        """Every article of the list with its whole score for a person with these bonus
        categories, ascending by (score, id): the live query's order, read backwards.

        The score is `person_score` exactly: the shared part floored once, plus whole points."""
        if not self._scaled and self.candidates:
            # Worked out in full before it is kept, so a failure leaves nothing half made.
            scaled = [scaled_base(c.base) for c in self.candidates]
            self._ids.extend(c.id for c in self.candidates)
            self._categories.extend(c.category for c in self.candidates)
            self._scaled.extend(scaled)
        points = {
            category: INTEREST_POINTS * (category in interest_categories)
            + LEVEL_POINTS * (category in level_categories)
            for category in interest_categories | level_categories
        }
        bonuses = [points.get(category, 0) for category in self._categories]
        return sorted(zip(map(int.__add__, self._scaled, bonuses), self._ids, strict=True))


def key_for(org: uuid.UUID, profession: str, moment: datetime) -> str | None:
    """The key of one ranking, or None when the profession code is not one we would put in a key."""
    if not _PROFESSION.fullmatch(profession):
        return None
    return f"{KEY_PREFIX}:{org}:{profession}:{int(moment.timestamp())}"


def _encode(ranking: Ranking) -> str:
    value: dict[str, object] = {
        "c": ranking.complete,
        "e": [[str(c.id), c.category, str(c.base)] for c in ranking.candidates],
    }
    if ranking.stamp is not None:
        value["n"] = ranking.stamp
    return json.dumps(value, separators=(",", ":"))


def _decode(raw: str | bytes) -> Ranking | None:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("c"), bool):
            return None
        stamp = data.get("n")
        if stamp is not None and not (isinstance(stamp, str) and _STAMP.fullmatch(stamp)):
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
            shared = Decimal(base)
            # The shared part of a score is between 0 and 1. Anything else (NaN, infinity, a
            # huge exponent) would break the arithmetic of every request that reads the list,
            # and a parsed list stays in the process (F-50 review).
            if not (shared.is_finite() and _LOWEST <= shared <= _HIGHEST):
                return None
            found.append(Candidate(id=uuid.UUID(str(ident)), category=category, base=shared))
        return Ranking(candidates=found, complete=data["c"], stamp=stamp)
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
        local_entries: int = LOCAL_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        self._size = size
        self._ttl = ttl_seconds
        self._wait = wait_seconds
        self._local_entries = local_entries
        self._clock = clock
        # key -> (when the copy is dropped, on `clock`; the ranking)
        self._local: OrderedDict[str, tuple[float, Ranking]] = OrderedDict()

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
        found = await self.lookup(key, rank, make=make)
        return None if found is None else found[0]

    async def lookup(
        self,
        key: str,
        rank: Callable[[int], Awaitable[list[Candidate]]],
        *,
        make: bool = True,
        stand_in: str | None = None,
    ) -> tuple[Ranking, bool] | None:
        """`get_or_rank`, and the key of a ranking that may stand in for this one (F-50).

        While another request is making the ranking under `key`, the one under `stand_in` (the
        previous period's, for a first page) is answered at once if it is kept, instead of waiting
        up to `WAIT_SECONDS` holding a database connection. The flag says it stood in: the caller
        must then rank at the stand-in's moment."""
        try:
            found = await self._read(key)
            if found is not None:
                return found, False
            if not make:
                return None
            lock = f"{key}:lock"
            token = uuid.uuid4().hex
            if await self._redis.set(lock, token, nx=True, ex=LOCK_SECONDS):
                try:
                    return await self._make(key, rank), False
                finally:
                    # Only the one that took the lock lets it go: after LOCK_SECONDS it may be
                    # somebody else's.
                    if await self._redis.get(lock) in (token, token.encode()):
                        await self._redis.delete(lock)
            if stand_in is not None:
                previous = await self._read(stand_in)
                if previous is not None:
                    return previous, True
            # Another request is making it: wait for it a little. If it is not there by then, this
            # request answers with the live query for its own page: making the whole ranking again
            # would add the most work when the database is busiest.
            clock = asyncio.get_running_loop().time
            deadline = clock() + self._wait  # real time: a slow Redis answer counts too
            while clock() < deadline:
                await asyncio.sleep(POLL_SECONDS)
                found = await self._read(key)
                if found is not None:
                    return found, False
            return None
        except (RedisError, OSError, TimeoutError):
            logger.warning("feed ranking cache unavailable; ranking without it")
            return None

    async def _read(self, key: str) -> Ranking | None:
        kept = self._local.get(key)
        if kept is not None:
            until, copy = kept
            # 32 bytes instead of 150 KB: the parsed copy is used only while Redis still holds
            # this making of the ranking. Deleted, expired or made again: read it again.
            if (
                until > self._clock()
                and copy.stamp is not None
                and await self._redis.get(f"{key}:n") in (copy.stamp, copy.stamp.encode())
            ):
                self._local.move_to_end(key)
                return copy
            del self._local[key]
        raw = await self._redis.get(key)
        if raw is None:
            return None
        ranking = _decode(raw)
        if ranking is None:
            logger.warning("feed ranking cache: an unreadable value was ignored")
            return None
        self._keep(key, ranking)
        return ranking

    def _keep(self, key: str, ranking: Ranking) -> None:
        # At most as long as Redis keeps a ranking, counted from now; and only while Redis holds
        # the same mark (see `_read`).
        self._local[key] = (self._clock() + self._ttl, ranking)
        self._local.move_to_end(key)
        while len(self._local) > self._local_entries:
            self._local.popitem(last=False)

    async def _make(self, key: str, rank: Callable[[int], Awaitable[list[Candidate]]]) -> Ranking:
        candidates = await rank(self._size)
        stamp = uuid.uuid4().hex
        ranking = Ranking(candidates=candidates, complete=len(candidates) < self._size, stamp=stamp)
        # The value first, then its mark: a reader that sees the new value before the mark only
        # reads the value once more.
        await self._redis.set(key, _encode(ranking), ex=self._ttl)
        await self._redis.set(f"{key}:n", stamp, ex=self._ttl)
        self._keep(key, ranking)
        return ranking
