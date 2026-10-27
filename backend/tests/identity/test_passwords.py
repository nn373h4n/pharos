import asyncio
import threading
import time

import pytest

from pharos.identity.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordHasher,
    PasswordPolicyError,
    check_new_password,
)
from pharos.identity.repository import is_valid_username, normalize_username

CHEAP = {"memory_kib": 64, "time_cost": 1}
GOOD = "correct horse battery staple"


# --- policy ------------------------------------------------------------------------------------


def varied(length: int) -> str:
    return ("abcdefghijklmnopqrstuvwxyz" * 6)[:length]


def test_length_limits_are_inclusive() -> None:
    check_new_password(varied(MIN_PASSWORD_LENGTH))
    check_new_password(varied(MAX_PASSWORD_LENGTH))
    for length in (MIN_PASSWORD_LENGTH - 1, MAX_PASSWORD_LENGTH + 1, 0):
        with pytest.raises(PasswordPolicyError, match="characters"):
            check_new_password(varied(length))


def test_unicode_counts_characters_not_bytes() -> None:
    check_new_password("əçğıöşüəçğıöşüa")  # noqa: RUF001  (15 characters, 29 bytes)
    check_new_password("пароль-пароль-123")


def test_no_composition_rules_are_imposed() -> None:
    check_new_password("alllowercaseletters")
    check_new_password("19283746501928374650")


@pytest.mark.parametrize(
    "password",
    [
        "a" * 20,
        "ab" * 10,
        "abcabcabcabcabcabc",
        "PasswordPassword",
        "Password 12345678",
        "1234567890123456",
    ],
)
def test_obvious_patterns_and_common_passwords_are_refused(password: str) -> None:
    with pytest.raises(PasswordPolicyError, match="easy to guess"):
        check_new_password(password)


def test_a_nul_character_and_the_username_are_refused() -> None:
    with pytest.raises(PasswordPolicyError, match="NUL"):
        check_new_password("a" * 20 + "\x00")
    with pytest.raises(PasswordPolicyError, match="username"):
        check_new_password("Administrator-Account", username="administrator-account")


# --- usernames ---------------------------------------------------------------------------------


def test_usernames_are_case_folded_trimmed_and_nfkc_normalized() -> None:
    assert normalize_username("  Alice.Smith@Example.COM ") == "alice.smith@example.com"
    fullwidth = "\uff21\uff2c\uff29\uff23\uff25"  # full-width "ALICE"
    assert normalize_username(fullwidth) == "alice"


@pytest.mark.parametrize("name", ["alice", "a", "a.b-c_d+e@f.g", "0day", "x" * 128])
def test_valid_usernames(name: str) -> None:
    assert is_valid_username(name)


@pytest.mark.parametrize(
    "name", ["", "-lead", ".lead", "has space", "ünïcode", "x" * 129, "new\nline", "a/b", "a;b"]
)
def test_invalid_usernames(name: str) -> None:
    assert not is_valid_username(name)


# --- hashing -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_hash_is_argon2id_and_verifies_only_the_right_password() -> None:
    hasher = PasswordHasher(**CHEAP)
    stored = await hasher.hash(GOOD)
    assert stored.startswith("$argon2id$")  # also what the database constraint demands
    assert GOOD not in stored
    assert (await hasher.verify(stored, GOOD)).ok
    assert not (await hasher.verify(stored, GOOD + "!")).ok
    assert not (await hasher.verify(stored, "")).ok
    assert await hasher.hash(GOOD) != stored  # a fresh salt every time


@pytest.mark.anyio
async def test_a_missing_hash_does_the_work_and_always_fails() -> None:
    hasher = PasswordHasher(**CHEAP)
    calls: list[str] = []
    original = hasher._check

    def spy(target: str, password: str) -> bool:
        calls.append(target)
        return original(target, password)

    hasher._check = spy  # type: ignore[method-assign]
    result = await hasher.verify(None, GOOD)
    assert not result.ok
    assert len(calls) == 1  # a real Argon2 verification ran against the dummy
    assert calls[0].startswith("$argon2id$")
    # Even the dummy's own secret cannot be guessed into a success: stored=None never succeeds.
    assert not (await hasher.verify(None, "")).ok


@pytest.mark.anyio
@pytest.mark.parametrize("garbage", ["", "not a hash", "$argon2id$broken", "$2b$12$bcrypt"])
async def test_a_damaged_stored_hash_is_a_failure_not_an_error(garbage: str) -> None:
    hasher = PasswordHasher(**CHEAP)
    assert not (await hasher.verify(garbage or None, GOOD)).ok


@pytest.mark.anyio
async def test_a_weaker_stored_hash_asks_for_a_rehash() -> None:
    weak = PasswordHasher(memory_kib=8, time_cost=1)
    strong = PasswordHasher(memory_kib=64, time_cost=2)
    stored = await weak.hash(GOOD)
    result = await strong.verify(stored, GOOD)
    assert result.ok
    assert result.needs_rehash
    assert not (await strong.verify(await strong.hash(GOOD), GOOD)).needs_rehash
    assert not (await strong.verify(stored, "wrong password!!")).needs_rehash


@pytest.mark.anyio
async def test_only_a_few_hashes_run_at_once() -> None:
    hasher = PasswordHasher(concurrency=2, **CHEAP)
    running = 0
    peak = 0
    lock = threading.Lock()
    original = hasher._check

    def slow(target: str, password: str) -> bool:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return original(target, password)

    hasher._check = slow  # type: ignore[method-assign]
    stored = await hasher.hash(GOOD)
    await asyncio.gather(*(hasher.verify(stored, GOOD) for _ in range(8)))
    assert peak == 2


@pytest.mark.anyio
async def test_hashing_does_not_block_the_event_loop() -> None:
    hasher = PasswordHasher(memory_kib=32_768, time_cost=2)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await hasher.hash(GOOD)
    finally:
        task.cancel()
    assert ticks >= 3


@pytest.mark.anyio
async def test_a_stronger_stored_hash_is_never_rewritten_with_weaker_parameters() -> None:
    strong = PasswordHasher(memory_kib=128, time_cost=3)
    cheaper_today = PasswordHasher(memory_kib=64, time_cost=2)
    stored = await strong.hash(GOOD)
    result = await cheaper_today.verify(stored, GOOD)
    assert result.ok
    assert not result.needs_rehash  # lowering the configured cost must not downgrade anyone


@pytest.mark.anyio
@pytest.mark.parametrize(("memory", "time_cost"), [(32, 2), (64, 1)])
async def test_each_weaker_parameter_asks_for_a_rehash(memory: int, time_cost: int) -> None:
    today = PasswordHasher(memory_kib=64, time_cost=2)
    stored = await PasswordHasher(memory_kib=memory, time_cost=time_cost).hash(GOOD)
    assert (await today.verify(stored, GOOD)).needs_rehash


@pytest.mark.anyio
async def test_a_saturated_hasher_fails_with_unavailable_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pharos.core.errors import DependencyUnavailable
    from pharos.identity import passwords

    monkeypatch.setattr(passwords, "QUEUE_TIMEOUT_SECONDS", 0.05)
    hasher = PasswordHasher(concurrency=1, **CHEAP)
    await hasher._semaphore.acquire()  # every slot is busy
    try:
        # A regression would wait for ever; the outer limit makes it a failure, not a hang.
        with pytest.raises(DependencyUnavailable):
            await asyncio.wait_for(hasher.hash(GOOD), 5)
        with pytest.raises(DependencyUnavailable):
            await asyncio.wait_for(hasher.verify(None, GOOD), 5)
    finally:
        hasher._semaphore.release()
    assert (await hasher.verify(await hasher.hash(GOOD), GOOD)).ok  # and it recovers


@pytest.mark.anyio
async def test_slots_are_released_even_when_hashing_fails() -> None:
    hasher = PasswordHasher(concurrency=1, **CHEAP)

    def boom(*_: object) -> bool:
        raise RuntimeError("boom")

    hasher._check = boom  # type: ignore[method-assign]
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await hasher.verify(None, GOOD)
    assert hasher._semaphore._value == 1
