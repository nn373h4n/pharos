import pytest

from pharos.core.heartbeat import (
    HEARTBEAT_KEY,
    HEARTBEAT_MAX_AGE_SECONDS,
    record_heartbeat,
    worker_is_alive,
)

NOW = 1_800_000_000.0


class FakeSyncRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def set(self, name: str, value: str, ex: int) -> None:
        self.calls.append((name, value, ex))


class FakeAsyncRedis:
    def __init__(self, value: bytes | str | None) -> None:
        self._value = value

    async def get(self, name: str) -> bytes | str | None:
        assert name == HEARTBEAT_KEY
        return self._value


def test_record_heartbeat_stores_the_time_with_an_expiry() -> None:
    client = FakeSyncRedis()
    record_heartbeat(client, NOW)
    [(name, value, expiry)] = client.calls
    assert name == HEARTBEAT_KEY
    assert float(value) == NOW
    assert expiry > HEARTBEAT_MAX_AGE_SECONDS  # the key outlives the "alive" window


@pytest.mark.anyio
@pytest.mark.parametrize("age", [0, 30, HEARTBEAT_MAX_AGE_SECONDS])
async def test_a_recent_heartbeat_means_the_worker_is_alive(age: float) -> None:
    assert await worker_is_alive(FakeAsyncRedis(repr(NOW - age)), NOW)


@pytest.mark.anyio
async def test_redis_bytes_are_accepted() -> None:
    assert await worker_is_alive(FakeAsyncRedis(repr(NOW - 5).encode()), NOW)


@pytest.mark.anyio
@pytest.mark.parametrize("age", [HEARTBEAT_MAX_AGE_SECONDS + 1, 3600])
async def test_an_old_heartbeat_means_the_worker_is_down(age: float) -> None:
    assert not await worker_is_alive(FakeAsyncRedis(repr(NOW - age)), NOW)


@pytest.mark.anyio
async def test_a_small_clock_difference_is_tolerated_but_a_future_timestamp_is_not() -> None:
    assert await worker_is_alive(FakeAsyncRedis(repr(NOW + 3)), NOW)
    assert not await worker_is_alive(FakeAsyncRedis(repr(NOW + 600)), NOW)


@pytest.mark.anyio
@pytest.mark.parametrize("value", [None, "", "not-a-number", b"\xff\xfe"])
async def test_a_missing_or_unreadable_heartbeat_means_the_worker_is_down(
    value: bytes | str | None,
) -> None:
    assert not await worker_is_alive(FakeAsyncRedis(value), NOW)
