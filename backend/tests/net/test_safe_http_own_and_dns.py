"""F-20: the installation's own addresses are refused whatever name leads to them, and a name
server that never answers cannot tie up the process."""

import asyncio
import socket
import threading
import time

import httpcore
import pytest

from pharos.net.addresses import AddressPolicy
from pharos.net.safe_http import (
    FetchError,
    FetchErrorKind,
    OwnAddresses,
    SafeHttpClient,
    SystemResolver,
)

from .fakes import OTHER_PUBLIC, PUBLIC, FakeResolver, Script, response

OWN_IP = "203.0.113.50"  # the public address of this installation (documentation range)
ALLOW = AddressPolicy(
    __import__("pharos.net.addresses", fromlist=["parse_networks"]).parse_networks(
        ["203.0.113.0/24"]
    )
)


def client(table: dict[str, list[str]], *answers: bytes, ttl_clock=None):  # type: ignore[no-untyped-def]
    script = Script(*answers)
    resolver = FakeResolver(dict(table))  # type: ignore[arg-type]
    built = SafeHttpClient(
        ALLOW, resolver=resolver, backend=script, own_hosts=frozenset({"pharos.example"})
    )
    return built, script, resolver


# --- the own addresses --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_another_name_for_the_own_address_is_refused_and_a_control_name_is_fetched() -> None:
    table = {"pharos.example": [OWN_IP], "alias.example": [OWN_IP], "feed.example": [PUBLIC]}
    built, script, _ = client(table, response(200, b"ok"))
    # The control: an unrelated name is fetched.
    assert (await built.fetch("https://feed.example/")).status == 200
    with pytest.raises(FetchError) as caught:
        await built.fetch("https://alias.example/")
    assert caught.value.kind is FetchErrorKind.BLOCKED
    assert script.connects == [(PUBLIC, 443)]  # nothing was opened to the own address


@pytest.mark.anyio
async def test_one_own_address_among_several_blocks_the_name() -> None:
    table = {"pharos.example": [OWN_IP], "mixed.example": [PUBLIC, OWN_IP]}
    built, script, _ = client(table)
    with pytest.raises(FetchError) as caught:
        await built.fetch("https://mixed.example/")
    assert caught.value.kind is FetchErrorKind.BLOCKED and script.connects == []


@pytest.mark.anyio
async def test_the_own_address_in_another_spelling_is_refused() -> None:
    table = {"pharos.example": [OWN_IP], "v.example": ["::ffff:203.0.113.50"]}
    built, script, _ = client(table)
    with pytest.raises(FetchError):
        await built.fetch("https://v.example/")
    assert script.connects == []


@pytest.mark.anyio
async def test_the_own_addresses_are_looked_up_again_after_the_ttl() -> None:
    now = [0.0]
    resolver = FakeResolver({"pharos.example": lambda n: [OWN_IP] if n == 1 else [OTHER_PUBLIC]})
    own = OwnAddresses(resolver, ["pharos.example"], ttl=60.0, clock=lambda: now[0])
    assert await own.get() == frozenset({OWN_IP})
    now[0] = 59.0
    assert await own.get() == frozenset({OWN_IP})  # the control: inside the minute, no new lookup
    assert resolver.calls.count("pharos.example") == 1
    now[0] = 61.0
    assert await own.get() == frozenset({OTHER_PUBLIC})


@pytest.mark.anyio
async def test_when_the_own_name_cannot_be_resolved_the_last_answer_is_kept() -> None:
    now = [0.0]
    answers: list[list[str] | None] = [[OWN_IP], None]

    class Flaky:
        async def resolve(self, host: str, port: int) -> list[str]:
            answer = answers[0] if len(answers) == 1 else answers.pop(0)
            if answer is None:
                raise httpcore.ConnectError("down")
            return answer

    own = OwnAddresses(Flaky(), ["pharos.example"], ttl=10.0, clock=lambda: now[0])
    assert await own.get() == frozenset({OWN_IP})
    now[0] = 11.0
    assert await own.get() == frozenset({OWN_IP})  # the lookup failed; the old answer stands


# --- the name server that never answers ---------------------------------------------------------


@pytest.mark.anyio
async def test_a_lookup_that_never_returns_gives_up_and_does_not_block_the_default_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    ran_on: list[str] = []

    def hang(*_: object, **__: object) -> list[object]:
        ran_on.append(threading.current_thread().name)
        release.wait(10)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", hang)
    resolver = SystemResolver(timeout=0.2)
    started = time.monotonic()
    with pytest.raises(httpcore.ConnectError):
        await resolver.resolve("hang.example", 443)
    assert time.monotonic() - started < 2
    # The default thread pool of the process still works: the stuck lookup is not on it.
    loop = asyncio.get_running_loop()
    assert await asyncio.wait_for(loop.run_in_executor(None, lambda: 42), 2) == 42
    # And the stuck lookup is on the threads kept for DNS, not on the shared ones.
    assert ran_on and all(name.startswith("pharos-dns") for name in ran_on)
    release.set()


@pytest.mark.anyio
async def test_the_real_resolver_still_resolves_a_literal_address() -> None:
    assert await SystemResolver().resolve("127.0.0.1", 80) == ["127.0.0.1"]


@pytest.mark.anyio
async def test_from_settings_passes_the_public_name_so_its_addresses_are_refused() -> None:
    from pharos.core.config import Settings

    settings = Settings(
        environment="test",
        public_url="https://pharos.example",
        fetch_test_allowed_networks="203.0.113.0/24",
    )
    table = {"pharos.example": [OWN_IP], "alias.example": [OWN_IP], "feed.example": [PUBLIC]}
    script = Script(response(200, b"ok"))
    built = SafeHttpClient.from_settings(
        settings,
        resolver=FakeResolver(dict(table)),
        backend=script,  # type: ignore[arg-type]
    )
    assert (await built.fetch("https://feed.example/")).status == 200  # the control
    with pytest.raises(FetchError) as caught:
        await built.fetch("https://alias.example/")
    assert caught.value.kind is FetchErrorKind.BLOCKED


@pytest.mark.anyio
async def test_a_failed_first_lookup_is_tried_again_after_ten_seconds_not_a_minute() -> None:
    now = [0.0]
    attempts: list[int] = []

    class LateDns:
        async def resolve(self, host: str, port: int) -> list[str]:
            attempts.append(1)
            if len(attempts) == 1:
                raise httpcore.ConnectError("down")
            return [OWN_IP]

    own = OwnAddresses(LateDns(), ["pharos.example"], ttl=60.0, clock=lambda: now[0])
    assert await own.get() == frozenset()  # nothing known yet: the check is empty, not an error
    now[0] = 5.0
    assert await own.get() == frozenset() and len(attempts) == 1  # too soon to try again
    now[0] = 11.0
    assert await own.get() == frozenset({OWN_IP})  # tried again well before the minute


def test_clients_built_with_the_real_resolver_share_one_answer_for_the_process() -> None:
    from pharos.net.safe_http import _process_wide_own

    first_resolver, first = _process_wide_own(["shared-a.example"])
    second_resolver, second = _process_wide_own(["shared-a.example"])
    _, other = _process_wide_own(["shared-b.example"])
    assert first is second and first_resolver is second_resolver
    assert other is not first  # a control: a different set of names is a different entry
