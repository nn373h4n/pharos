"""The host gate on the real Redis, as the worker's own Redis user (F-27): a hold closes the gate
for as long as the host asked, the remaining wait can be read, and nothing leaves the worker's
prefix."""

import uuid

import pytest
from redis.asyncio import Redis

from pharos.ingest.hostgate import KEY_PREFIX, HostGate, _name
from pharos.net.safe_http import HOST_HELD, FetchError

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_a_hold_closes_the_gate_and_says_how_long_as_the_worker_user(
    redis_worker_url: str,
) -> None:
    redis = Redis.from_url(redis_worker_url, decode_responses=True)
    host = f"held-{uuid.uuid4().hex}.example"
    other = f"open-{uuid.uuid4().hex}.example"
    try:
        gate = HostGate(redis, gap_seconds=5)
        assert await gate.wait_seconds(host) == 0  # open: nothing to wait for
        await gate.hold(host, 120)
        assert await gate.enter(host) is False
        assert 118 <= await gate.wait_seconds(host) <= 120
        assert await gate.enter(other) is True  # another host is not held
        assert 1 <= await gate.wait_seconds(other) <= 5  # it has the usual short gap
        # The worker's user may not list keys (no SCAN): the two keys are looked up by name.
        for name in (_name(host), _name(other)):
            assert name.startswith(KEY_PREFIX) and KEY_PREFIX.startswith("pharos:worker:")
            assert await redis.exists(name) == 1
    finally:
        await redis.delete(_name(host), _name(other))
        await redis.aclose()


async def test_a_redirect_waits_out_the_gap_and_a_held_host_ends_it_as_the_worker_user(
    redis_worker_url: str,
) -> None:
    """F-23 on the real Redis: the wait is the key's own expiry, read with PTTL."""
    redis = Redis.from_url(redis_worker_url, decode_responses=True)
    busy = f"busy-{uuid.uuid4().hex}.example"
    held = f"held-{uuid.uuid4().hex}.example"
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        await redis.delete(_name(busy))  # the gap runs out while the fetch waits

    try:
        gate = HostGate(redis, gap_seconds=5, sleep=sleep)
        assert await gate.enter(busy) is True
        await gate.pass_hop(busy)
        assert len(waits) == 1 and 1 <= waits[0] <= 5
        assert 1 <= await gate.wait_seconds(busy) <= 5  # the hop closed the gate again
        await gate.hold(held, 300)
        with pytest.raises(FetchError) as caught:
            await gate.pass_hop(held)
        assert caught.value.reason == HOST_HELD and 298 <= (caught.value.retry_after or 0) <= 300
        assert len(waits) == 1
    finally:
        await redis.delete(_name(busy), _name(held))
        await redis.aclose()
