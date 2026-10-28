"""Server-side sessions in Redis (ADR-0003, ADR-0011).

The cookie holds an opaque 256-bit id; Redis holds only who the session belongs to (user,
organization, when they authenticated) and a CSRF seed. The role and the account status are never
stored here: they are read from the `users` row on every request, so a demotion or a disabled
account applies at once.

Redis keys use the SHA-256 of the id, so a Redis dump or a list of keys does not reveal usable
cookies. A per-user index lets "disable user", "role changed" and "sign out everywhere" delete every
session of one user. The idle timeout slides; the absolute limit does not. If Redis is unreachable
every call raises `SessionStoreUnavailable` and the request fails closed (503), never open.
"""

import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from pharos.core.auth import SessionIdentity
from pharos.core.clock import Clock
from pharos.core.errors import DependencyUnavailable

# secrets.token_urlsafe(32) is 43 characters of this alphabet. Anything else is not ours and is
# rejected before it reaches Redis.
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{43}")
KEY_PREFIX = "pharos:session:"


class SessionStoreUnavailable(DependencyUnavailable):
    """Redis could not be reached or answered with an error."""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    user_id: uuid.UUID
    org_id: uuid.UUID
    auth_time: datetime
    created_at: datetime
    csrf_seed: str
    # How the person signed in: "local" (password) or "oidc" (single sign-on).
    method: str = "local"

    def dumps(self) -> str:
        return json.dumps(
            {
                "user_id": str(self.user_id),
                "org_id": str(self.org_id),
                "auth_time": self.auth_time.timestamp(),
                "created_at": self.created_at.timestamp(),
                "csrf_seed": self.csrf_seed,
                "method": self.method,
            }
        )

    @classmethod
    def loads(cls, raw: bytes | str) -> "SessionRecord":
        data = json.loads(raw)
        return cls(
            user_id=uuid.UUID(data["user_id"]),
            org_id=uuid.UUID(data["org_id"]),
            auth_time=datetime.fromtimestamp(data["auth_time"], UTC),
            created_at=datetime.fromtimestamp(data["created_at"], UTC),
            csrf_seed=str(data["csrf_seed"]),
            method=str(data.get("method", "local")),
        )


def is_well_formed(session_id: str) -> bool:
    return _SESSION_ID.fullmatch(session_id) is not None


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


# Each script runs atomically inside Redis, so a revoke ("sign out everywhere") can never fall
# between the steps of a create or a rotation and leave a live session behind.

# KEYS: session key, user index. ARGV: record, idle ttl, digest, index ttl, key prefix.
# Also forgets index entries whose session has already expired, so the index cannot grow forever.
_CREATE = """
for _, d in ipairs(redis.call('SMEMBERS', KEYS[2])) do
  if redis.call('EXISTS', ARGV[5] .. 'id:' .. d) == 0 then redis.call('SREM', KEYS[2], d) end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('SADD', KEYS[2], ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[4])
return 1
"""

# KEYS: old key, new key, user index. ARGV: record, ttl, old digest, new digest, index ttl.
# Does nothing (returns 0) when the old session is already gone, for example revoked a moment ago.
_ROTATE = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[3], ARGV[3])
redis.call('SADD', KEYS[3], ARGV[4])
redis.call('EXPIRE', KEYS[3], ARGV[5])
return 1
"""

# KEYS: user index. ARGV: key prefix. Returns how many sessions were deleted.
_REVOKE = """
local n = 0
for _, d in ipairs(redis.call('SMEMBERS', KEYS[1])) do
  n = n + redis.call('DEL', ARGV[1] .. 'id:' .. d)
end
redis.call('DEL', KEYS[1])
return n
"""


class SessionStore:
    def __init__(
        self,
        redis: Redis,
        clock: Clock,
        *,
        idle_seconds: int,
        absolute_seconds: int,
        prefix: str = KEY_PREFIX,
    ) -> None:
        self._redis = redis
        self._clock = clock
        self._idle = idle_seconds
        self._absolute = absolute_seconds
        self._prefix = prefix
        self._create_script = redis.register_script(_CREATE)
        self._rotate_script = redis.register_script(_ROTATE)
        self._revoke_script = redis.register_script(_REVOKE)

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}id:{_digest(session_id)}"

    def _index(self, user_id: uuid.UUID) -> str:
        return f"{self._prefix}user:{user_id}"

    def _ttl(self, record: SessionRecord) -> int:
        """Idle timeout, but never past the absolute limit."""
        age = (self._clock.now() - record.created_at).total_seconds()
        return max(1, min(self._idle, int(self._absolute - age)))

    async def create(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        auth_time: datetime | None = None,
        *,
        method: str = "local",
    ) -> tuple[str, SessionRecord]:
        now = self._clock.now()
        record = SessionRecord(
            user_id=user_id,
            org_id=org_id,
            auth_time=auth_time or now,
            created_at=now,
            csrf_seed=secrets.token_hex(16),
            method=method,
        )
        session_id = secrets.token_urlsafe(32)
        try:
            await self._create_script(
                keys=[self._key(session_id), self._index(user_id)],
                args=[
                    record.dumps(),
                    self._ttl(record),
                    _digest(session_id),
                    self._absolute + 3600,
                    self._prefix,
                ],
            )
        except RedisError as error:
            raise SessionStoreUnavailable from error
        return session_id, record

    async def get(self, session_id: str) -> SessionRecord | None:
        """The session behind a cookie value, or None. Extends the idle timeout."""
        if not is_well_formed(session_id):
            return None
        key = self._key(session_id)
        try:
            raw: Any = await self._redis.get(key)
            if raw is None:
                return None
            try:
                record = SessionRecord.loads(raw)
            except (ValueError, KeyError, TypeError):
                await self._redis.delete(key)
                return None
            age = (self._clock.now() - record.created_at).total_seconds()
            if age >= self._absolute:
                await self._forget(session_id, record.user_id)
                return None
            # EXPIRE answers 0 when the key is gone: a session revoked between the read and now
            # must not be accepted one last time.
            if not await self._redis.expire(key, self._ttl(record)):
                return None
        except RedisError as error:
            raise SessionStoreUnavailable from error
        return record

    async def _forget(self, session_id: str, user_id: uuid.UUID) -> None:
        await self._redis.delete(self._key(session_id))
        await self._redis.srem(self._index(user_id), _digest(session_id))

    async def delete(self, session_id: str) -> None:
        if not is_well_formed(session_id):
            return
        try:
            raw: Any = await self._redis.get(self._key(session_id))
            if raw is None:
                return
            try:
                await self._forget(session_id, SessionRecord.loads(raw).user_id)
            except (ValueError, KeyError, TypeError):
                await self._redis.delete(self._key(session_id))
        except RedisError as error:
            raise SessionStoreUnavailable from error

    async def rotate(self, session_id: str) -> tuple[str, SessionRecord] | None:
        """A new id for the same session (sign-in, privilege change). The old id stops working."""
        record = await self.get(session_id)
        if record is None:
            return None
        # A new CSRF seed too: a token captured before the change must not survive it.
        fresh = SessionRecord(
            user_id=record.user_id,
            org_id=record.org_id,
            auth_time=record.auth_time,
            created_at=record.created_at,
            csrf_seed=secrets.token_hex(16),
            method=record.method,
        )
        new_id = secrets.token_urlsafe(32)
        try:
            done = await self._rotate_script(
                keys=[self._key(session_id), self._key(new_id), self._index(record.user_id)],
                args=[
                    fresh.dumps(),
                    self._ttl(fresh),
                    _digest(session_id),
                    _digest(new_id),
                    self._absolute + 3600,
                ],
            )
        except RedisError as error:
            raise SessionStoreUnavailable from error
        return (new_id, fresh) if done else None

    async def revoke_user(self, user_id: uuid.UUID) -> int:
        """Delete every session of one user. Returns how many were removed."""
        try:
            removed = await self._revoke_script(keys=[self._index(user_id)], args=[self._prefix])
        except RedisError as error:
            raise SessionStoreUnavailable from error
        return int(removed)


class SessionIdentityResolver:
    """Turns the session cookie into an identity for `current_principal`."""

    def __init__(self, store: SessionStore, cookie_name: str, *, allow_local: bool = True) -> None:
        self._store = store
        self._cookie_name = cookie_name
        self._allow_local = allow_local

    async def resolve(self, request: Any) -> SessionIdentity | None:
        session_id = request.cookies.get(self._cookie_name)
        if not session_id:
            return None
        record = await self._store.get(session_id)
        if record is None:
            return None
        if record.method == "local" and not self._allow_local:
            return None  # password sign-in was switched off: its sessions end with it
        request.state.session_id = session_id
        request.state.session = record
        return SessionIdentity(user_id=record.user_id, org_id=record.org_id)
