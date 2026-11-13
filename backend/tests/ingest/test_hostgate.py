"""One request per host in a short time, across all workers."""

import pytest

from pharos.ingest.hostgate import KEY_PREFIX, MAX_HOLD_SECONDS, HostGate

pytestmark = pytest.mark.anyio


class FakeRedis:
    def __init__(self) -> None:
        self.keys: dict[str, tuple[str, int]] = {}
        self.calls: list[tuple[str, bool, int]] = []

    async def set(self, name: str, value: str, *, nx: bool = False, px: int) -> bool | None:
        self.calls.append((name, nx, px))
        if nx and name in self.keys:
            return None
        self.keys[name] = (value, px)
        return True

    async def pttl(self, name: str) -> int:
        return self.keys[name][1] if name in self.keys else -2


async def test_the_first_fetch_for_a_host_goes_and_the_next_one_waits() -> None:
    gate = HostGate(FakeRedis(), gap_seconds=5)
    assert await gate.enter("news.example") is True
    assert await gate.enter("news.example") is False
    assert await gate.enter("other.example") is True  # another host is independent


async def test_hosts_are_compared_without_regard_to_case() -> None:
    gate = HostGate(FakeRedis())
    assert await gate.enter("News.Example") is True
    assert await gate.enter("news.example") is False


async def test_the_key_is_under_the_workers_own_prefix_and_expires() -> None:
    redis = FakeRedis()
    await HostGate(redis, gap_seconds=7).enter("news.example")
    name, nx, px = redis.calls[0]
    assert name.startswith(KEY_PREFIX) and name.startswith("pharos:worker:")
    assert (nx, px) == (True, 7000)
    assert "news.example" not in name  # the host is hashed, not stored


# --- F-27: a host that asked to wait closes its gate --------------------------------------------


async def test_a_host_that_asked_to_wait_is_closed_for_that_long() -> None:
    redis = FakeRedis()
    gate = HostGate(redis, gap_seconds=5)
    await gate.hold("api.github.example", 3600)
    assert await gate.enter("api.github.example") is False
    assert await gate.enter("API.github.example") is False  # the same host in any case
    assert await gate.enter("news.example") is True  # only that host
    assert await gate.wait_seconds("api.github.example") == 3600
    name, nx, px = redis.calls[0]
    assert name.startswith(KEY_PREFIX) and (nx, px) == (False, 3_600_000)


async def test_a_hold_replaces_the_short_gap_and_is_at_most_a_day() -> None:
    redis = FakeRedis()
    gate = HostGate(redis, gap_seconds=5)
    assert await gate.enter("news.example") is True  # the usual gap of five seconds ...
    await gate.hold("news.example", 10 * 24 * 3600)  # ... then the host asked for ten days
    assert await gate.wait_seconds("news.example") == MAX_HOLD_SECONDS == 24 * 3600


@pytest.mark.parametrize("seconds", [0, -5, 3, 5])
async def test_a_wait_no_longer_than_the_gap_changes_nothing(seconds: int) -> None:
    redis = FakeRedis()
    await HostGate(redis, gap_seconds=5).hold("news.example", seconds)
    assert redis.calls == []


async def test_an_open_gate_has_nothing_to_wait_for() -> None:
    gate = HostGate(FakeRedis(), gap_seconds=5)
    assert await gate.wait_seconds("news.example") == 0
    await gate.hold("news.example", 61)
    assert await gate.wait_seconds("news.example") == 61
