"""A scripted network and a fake resolver, so fetcher tests need no real network."""
# ruff: noqa: ASYNC109  (the httpcore interface names its parameter "timeout")

import asyncio
import ipaddress
from collections.abc import Callable

import httpcore

PUBLIC = "93.184.216.34"
OTHER_PUBLIC = "140.82.112.3"


def response(
    status: int = 200,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    *,
    length: bool = True,
) -> bytes:
    reason = {200: "OK", 301: "Moved", 302: "Found", 304: "Not Modified", 404: "Not Found"}.get(
        status, "Status"
    )
    lines = [f"HTTP/1.1 {status} {reason}"]
    merged = dict(headers or {})
    if length and "Content-Length" not in merged:
        merged["Content-Length"] = str(len(body))
    lines += [f"{k}: {v}" for k, v in merged.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def redirect(location: str, status: int = 302) -> bytes:
    return response(status, b"", {"Location": location})


class RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, buffer: list[bytes], *, hang: bool = False) -> None:
        super().__init__(buffer)
        self.written = b""
        self.tls_hostname: str | None = None
        self._hang = hang

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.written += buffer

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self._hang:
            await asyncio.sleep(3600)
        return await super().read(max_bytes, timeout)

    async def start_tls(
        self, ssl_context: object, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.AsyncNetworkStream:
        self.tls_hostname = server_hostname
        return self

    @property
    def text(self) -> str:
        return self.written.decode("latin-1")


class Script(httpcore.AsyncNetworkBackend):
    """Hands out one scripted answer per connection and remembers what it was asked."""

    def __init__(self, *answers: bytes | Callable[[], RecordingStream]) -> None:
        self._answers = list(answers)
        self.connects: list[tuple[str, int]] = []
        self.streams: list[RecordingStream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connects.append((host, port))
        answer = self._answers.pop(0)
        stream = answer() if callable(answer) else RecordingStream([answer])
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(
        self, *args: object, **kwargs: object
    ) -> httpcore.AsyncNetworkStream:
        raise AssertionError("a unix socket must never be opened")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeResolver:
    """host -> addresses, or a function of the call number (to model DNS rebinding)."""

    def __init__(self, table: dict[str, list[str] | Callable[[int], list[str]]]) -> None:
        self._table = table
        self.calls: list[str] = []

    async def resolve(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            pass
        else:  # like getaddrinfo, an address literal resolves to itself
            return [host.strip("[]")]
        entry = self._table.get(host)
        if entry is None:
            raise httpcore.ConnectError(f"no such host {host}")
        if callable(entry):
            return entry(self.calls.count(host))
        return list(entry)
