"""One request per host in a short time, across all workers."""

import pytest

from pharos.ingest.hostgate import KEY_PREFIX, HostGate

pytestmark = pytest.mark.anyio


class FakeRedis:
    def __init__(self) -> None:
        self.keys: dict[str, tuple[str, int]] = {}
        self.calls: list[tuple[str, bool, int]] = []

    async def set(self, name: str, value: str, *, nx: bool, px: int) -> bool | None:
        self.calls.append((name, nx, px))
        if nx and name in self.keys:
            return None
        self.keys[name] = (value, px)
        return True


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
