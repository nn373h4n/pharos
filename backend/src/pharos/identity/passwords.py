"""Password hashing for local accounts: Argon2id, off the event loop, a few at a time.

* Argon2id with a memory cost of 64 MiB by default. That is expensive on purpose, so at most
  `concurrency` hashes run at once (a semaphore): with the 512 MB container limit a burst of logins
  must queue, not exhaust memory. The work runs in a thread, so the event loop keeps serving.
* An unknown user, an SSO-only user or a missing credential is checked against a hash of nothing
  in particular (`verify(None, ...)`), so the work done does not reveal whether the account exists.
* `needs_rehash` is true only when the stored hash is WEAKER than today's parameters (less memory,
  fewer iterations, another variant). It is never true for a stronger or equal hash, so lowering the
  configured cost does not make every sign-in rewrite stronger hashes.
* Waiting for a hashing slot is bounded: when the queue is so long that a person would wait
  `QUEUE_TIMEOUT_SECONDS`, the request fails with 503 instead of hanging.
* New passwords must be 15 to 128 characters (NIST SP 800-63B: length over composition rules).
"""

import asyncio
import re
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher as Argon2
from argon2 import Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError

from pharos.core.errors import DependencyUnavailable

MIN_PASSWORD_LENGTH = 15
MAX_PASSWORD_LENGTH = 128
QUEUE_TIMEOUT_SECONDS = 10.0
# Fewer different characters than this is a pattern, not a passphrase ("aaaaaaaaaaaaaaa").
MIN_DISTINCT_CHARACTERS = 6
# Passwords that are long enough to pass the length rule and still the first thing anyone tries.
_COMMON = frozenset(
    {
        "passwordpassword",
        "password12345678",
        "123456789012345",
        "1234567890123456",
        "qwertyuiopasdfgh",
        "qwertyuiopasdfg",
        "iloveyouiloveyou",
        "administrator123",
        "administratoradmin",
        "letmeinletmein1",
        "welcome12345678",
        "changemechangeme",
    }
)


class PasswordPolicyError(ValueError):
    """The chosen password is not acceptable. The message is safe to show to the person."""


def check_new_password(password: str, *, username: str = "") -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"the password must have {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters"
        )
    if "\x00" in password:
        raise PasswordPolicyError("the password cannot contain a NUL character")
    if username and password.lower() == username.lower():
        raise PasswordPolicyError("the password cannot be the same as the username")
    if (
        len(set(password)) < MIN_DISTINCT_CHARACTERS
        or re.sub(r"\W", "", password.lower()) in _COMMON
    ):
        raise PasswordPolicyError("the password is too easy to guess")


@dataclass(frozen=True, slots=True)
class Verification:
    ok: bool
    needs_rehash: bool = False


class PasswordHasher:
    def __init__(
        self,
        *,
        memory_kib: int = 65_536,
        time_cost: int = 3,
        parallelism: int = 1,
        concurrency: int = 2,
    ) -> None:
        self._argon2 = Argon2(
            time_cost=time_cost,
            memory_cost=memory_kib,
            parallelism=parallelism,
            type=Type.ID,
        )
        self._semaphore = asyncio.Semaphore(concurrency)
        self._memory_kib = memory_kib
        self._time_cost = time_cost
        self._parallelism = parallelism
        # Made once, with today's parameters, from a random value nobody knows.
        self._dummy = self._argon2.hash(secrets.token_urlsafe(32))

    async def _slot(self) -> None:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), QUEUE_TIMEOUT_SECONDS)
        except TimeoutError:
            raise DependencyUnavailable("password hashing is saturated") from None

    async def hash(self, password: str) -> str:
        await self._slot()
        try:
            return await asyncio.to_thread(self._argon2.hash, password)
        finally:
            self._semaphore.release()

    async def verify(self, stored: str | None, password: str) -> Verification:
        """Check a password. `stored=None` does the same work against the dummy hash and fails."""
        target = stored or self._dummy
        await self._slot()
        try:
            ok = await asyncio.to_thread(self._check, target, password)
        finally:
            self._semaphore.release()
        if not ok or stored is None:
            return Verification(False)
        return Verification(True, self._is_weaker(stored))

    def _is_weaker(self, stored: str) -> bool:
        try:
            params = extract_parameters(stored)
        except InvalidHashError:
            return True
        return (
            params.type != Type.ID
            or params.memory_cost < self._memory_kib
            or params.time_cost < self._time_cost
            or params.parallelism < self._parallelism
        )

    def _check(self, target: str, password: str) -> bool:
        try:
            return self._argon2.verify(target, password)
        except (VerificationError, InvalidHashError):
            return False
