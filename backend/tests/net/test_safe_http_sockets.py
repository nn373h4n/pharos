"""SafeHttpClient over real sockets: a local server that misbehaves in the ways feeds do."""

import asyncio
import gzip
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress

import pytest

from pharos.core.config import Settings
from pharos.net.addresses import AddressPolicy
from pharos.net.safe_http import FetchError, FetchErrorKind, SafeHttpClient, SystemResolver

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _read_request(reader: asyncio.StreamReader) -> str:
    return (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")


class Server:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.handler: Handler = self._ok

    async def _ok(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self.handler(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            with suppress(Exception):
                writer.close()


@pytest.fixture
async def server() -> AsyncIterator[tuple[Server, int]]:
    fake = Server()
    listener = await asyncio.start_server(fake._serve, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        yield fake, port
    finally:
        listener.close()
        await listener.wait_closed()


def client_for(port: int, **options: object) -> SafeHttpClient:
    settings = Settings(
        environment="test",
        fetch_test_allowed_networks="127.0.0.1/32",
        fetch_test_allowed_ports=str(port),
    )
    return SafeHttpClient.from_settings(settings, **options)


@pytest.mark.anyio
async def test_a_plain_fetch_works_over_a_real_socket(server: tuple[Server, int]) -> None:
    _, port = server
    client = client_for(port)
    result = await client.fetch(f"http://127.0.0.1:{port}/feed")
    assert (result.status, result.body) == (200, b"ok")
    await client.aclose()


@pytest.mark.anyio
async def test_without_the_test_allowance_the_same_server_is_unreachable(
    server: tuple[Server, int],
) -> None:
    _, port = server
    client = SafeHttpClient(AddressPolicy())
    with pytest.raises(FetchError) as caught:
        await client.fetch(f"http://127.0.0.1:{port}/feed")
    assert caught.value.kind is FetchErrorKind.BLOCKED
    await client.aclose()


@pytest.mark.anyio
async def test_the_allowance_opens_only_the_named_address(server: tuple[Server, int]) -> None:
    _, port = server
    client = client_for(port)
    with pytest.raises(FetchError) as caught:
        await client.fetch(f"http://127.0.0.2:{port}/feed")
    assert caught.value.kind is FetchErrorKind.BLOCKED
    await client.aclose()


@pytest.mark.anyio
async def test_a_redirect_from_the_allowed_server_to_another_loopback_address_is_blocked(
    server: tuple[Server, int],
) -> None:
    fake, port = server

    async def redirecting(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        fake.requests.append(await _read_request(reader))
        location = f"http://127.0.0.2:{port}/"
        writer.write(
            f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode()
        )
        await writer.drain()

    fake.handler = redirecting
    client = client_for(port)
    with pytest.raises(FetchError) as caught:
        await client.fetch(f"http://127.0.0.1:{port}/")
    assert caught.value.kind is FetchErrorKind.BLOCKED
    assert len(fake.requests) == 1
    await client.aclose()


@pytest.mark.anyio
async def test_a_slow_drip_is_ended_by_the_total_deadline(server: tuple[Server, int]) -> None:
    fake, port = server

    async def dripping(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
        for _ in range(1000):
            writer.write(b"a")
            await writer.drain()
            await asyncio.sleep(0.1)  # each read succeeds well inside any per-read timeout

    fake.handler = dripping
    client = client_for(port, deadline_seconds=0.8)
    started = asyncio.get_running_loop().time()
    with pytest.raises(FetchError) as caught:
        await client.fetch(f"http://127.0.0.1:{port}/")
    assert caught.value.kind is FetchErrorKind.TIMEOUT
    assert asyncio.get_running_loop().time() - started < 3
    await client.aclose()


@pytest.mark.anyio
async def test_an_endless_body_is_cut_at_the_size_limit(server: tuple[Server, int]) -> None:
    fake, port = server

    async def endless(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
        for _ in range(10_000):
            writer.write(b"a" * 65536)
            await writer.drain()

    fake.handler = endless
    client = client_for(port, max_bytes=100_000)
    with pytest.raises(FetchError) as caught:
        await client.fetch(f"http://127.0.0.1:{port}/")
    assert caught.value.kind is FetchErrorKind.TOO_LARGE
    await client.aclose()


@pytest.mark.anyio
async def test_a_gzip_bomb_is_refused_without_being_inflated(server: tuple[Server, int]) -> None:
    fake, port = server
    bomb = gzip.compress(b"\0" * 50_000_000)

    async def gzipped(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Sent although the request said `Accept-Encoding: identity`: a hostile server does that.
        fake.requests.append(await _read_request(reader))
        head = f"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: {len(bomb)}\r\n\r\n"
        writer.write(head.encode() + bomb)
        await writer.drain()

    fake.handler = gzipped
    client = client_for(port)
    with pytest.raises(FetchError) as caught:
        await client.fetch(f"http://127.0.0.1:{port}/")
    assert (caught.value.kind, caught.value.reason) == (FetchErrorKind.BAD_CONTENT, "compressed")
    assert "Accept-Encoding: identity" in fake.requests[0]
    await client.aclose()


@pytest.mark.anyio
async def test_a_server_that_closes_without_answering_is_a_network_error(
    server: tuple[Server, int],
) -> None:
    fake, port = server

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)

    fake.handler = silent
    client = client_for(port)
    with pytest.raises(FetchError) as caught:
        await client.fetch(f"http://127.0.0.1:{port}/")
    assert caught.value.kind is FetchErrorKind.NETWORK
    await client.aclose()


@pytest.mark.anyio
async def test_a_closed_port_is_a_network_error() -> None:
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    client = client_for(port)
    with pytest.raises(FetchError) as caught:
        await client.fetch(f"http://127.0.0.1:{port}/")
    assert caught.value.kind is FetchErrorKind.NETWORK
    await client.aclose()


# --- names for loopback and numeric spellings, with the real resolver -----------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.1/",
        "http://0x7f.0.0.1/",
        "http://0177.0.0.1/",
        "http://2130706433/",
        "http://017700000001/",
        "http://0x7f000001/",
        "http://localhost/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0/",
        "http://0.0.0.0/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://[fd00::1]/",
    ],
)
async def test_every_spelling_of_an_internal_address_is_blocked_by_the_real_resolver(
    url: str,
) -> None:
    client = SafeHttpClient(AddressPolicy(), resolver=SystemResolver())
    with pytest.raises(FetchError) as caught:
        await client.fetch(url)
    # Either the address check refuses it, or the host cannot be parsed at all: never a connection.
    assert caught.value.kind in {FetchErrorKind.BLOCKED, FetchErrorKind.NETWORK}, url
    await client.aclose()
