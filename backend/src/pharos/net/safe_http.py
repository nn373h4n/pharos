"""The only way Pharos fetches things from the internet on behalf of a source (ADR-0006, ADR-0012).

A source URL is untrusted input: a feed address typed by a user, a link inside a feed, a redirect.
Any of them may point at something inside the installation (the database, Redis, the API, a cloud
metadata service). The defences, in order:

* The URL must be http or https, on port 80 or 443, without credentials, and not the host of this
  installation's own public address.
* The hostname is resolved by the connection layer itself, EVERY resolved address must be acceptable
  (`AddressPolicy`), and the socket is then opened to one of those validated addresses. There is no
  second lookup between the check and the connect, so DNS rebinding cannot swap the address. The TLS
  server name and the Host header stay the original hostname.
* Redirects are followed by hand, at most three, and each hop goes through all of the above again.
  `Authorization` is only ever sent to the host it was bound to.
* Responses are read raw (no decompression), capped in size, and the whole fetch has a total
  deadline, so a server that drips one byte a second cannot hold a worker.
* Environment proxies are ignored, HTTP/2 is off, and errors are coarse categories: whoever reads
  them cannot tell "blocked" from "no such host" or learn internal names.

`httpx` and `httpcore` are imported only here and in the identity-provider client (an import-linter
contract enforces it); code that fetches uses this class.
"""

import asyncio
import logging
import re
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from pharos.net.addresses import AddressPolicy, parse_networks

logger = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
DEADLINE_SECONDS = 20.0
MAX_RETRY_AFTER = 7 * 24 * 3600
CONNECT_TIMEOUT_SECONDS = 5.0
MAX_REDIRECTS = 3
ALLOWED_PORTS = {"http": 80, "https": 443}
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
USER_AGENT = "Pharos/0.1 (+https://github.com/nn373h4n/pharos; feed reader)"
# Request headers a caller may set. Everything else is ours (Host, Accept-Encoding, Authorization).
CALLER_HEADERS = frozenset({"accept", "if-none-match", "if-modified-since", "user-agent"})
RESPONSE_HEADERS = (
    "etag",
    "last-modified",
    "content-type",
    "retry-after",
    "content-length",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
)


class FetchErrorKind(StrEnum):
    BLOCKED = "blocked"  # the URL or an address it leads to is not allowed
    TIMEOUT = "timeout"
    TOO_LARGE = "too_large"
    BAD_STATUS = "bad_status"  # a status we do not accept, including too many redirects
    BAD_CONTENT = "bad_content"  # compressed, or otherwise not what we asked for
    NETWORK = "network"  # could not connect, TLS failed, connection dropped


class FetchError(Exception):
    """A fetch failed. `kind` is safe to show; `reason` is for logs and tests only."""

    def __init__(self, kind: FetchErrorKind, reason: str = "", retry_after: int | None = None):
        super().__init__(kind.value)
        self.kind = kind
        self.reason = reason
        self.retry_after = retry_after


class _Blocked(Exception):
    """Raised inside the connection layer; turned into FetchError(BLOCKED) by the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Bearer:
    """A credential bound to ONE host: it is sent there and nowhere else, redirects included."""

    host: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class FetchResult:
    status: int  # 200, or 304 when a conditional request found nothing new
    headers: Mapping[str, str]
    body: bytes
    url: str  # where the body finally came from, after redirects


class Resolver(Protocol):
    async def resolve(self, host: str, port: int) -> list[str]: ...


class SystemResolver:
    async def resolve(self, host: str, port: int) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=1)  # SOCK_STREAM
        except OSError as error:
            raise httpcore.ConnectError(f"cannot resolve {host}") from error
        addresses: list[str] = []
        for info in infos:
            address = str(info[4][0]).split("%", 1)[0]  # drop an IPv6 scope id
            if address not in addresses:
                addresses.append(address)
        return addresses


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Resolves, validates every address, and connects to a validated one: one step, no gap."""

    def __init__(
        self, inner: httpcore.AsyncNetworkBackend, resolver: Resolver, policy: AddressPolicy
    ) -> None:
        self._inner = inner
        self._resolver = resolver
        self._policy = policy

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109  (the httpcore interface names it so)
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await self._resolver.resolve(host, port)
        except httpcore.ConnectError:
            # Same kind as a refused address: the caller must not learn which internal names exist.
            raise _Blocked("unresolvable") from None
        reason = self._policy.violations(addresses)
        if reason is not None:
            raise _Blocked(reason)
        last: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout,
                    local_address,
                    socket_options,  # type: ignore[arg-type]
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last = error
        raise last if last is not None else _Blocked("no_address")  # unreachable: never empty

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        raise _Blocked("unix_socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _Transport(httpx.AsyncHTTPTransport):
    """httpx transport whose connection pool uses the pinned backend."""

    def __init__(self, backend: httpcore.AsyncNetworkBackend, context: ssl.SSLContext) -> None:
        super().__init__(verify=context, http2=False, retries=0, trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            max_connections=4,
            max_keepalive_connections=0,  # a fresh, freshly validated connection every time
            http1=True,
            http2=False,
            network_backend=backend,
        )


@dataclass(frozen=True, slots=True)
class _Target:
    url: str
    scheme: str
    host: str


def check_url(
    url: str, blocked_hosts: frozenset[str], extra_ports: frozenset[int] = frozenset()
) -> _Target:
    """The URL-level rules. Address-level rules are applied when the connection is made."""
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 0x21 for c in url):
        raise FetchError(FetchErrorKind.BLOCKED, "url_malformed")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise FetchError(FetchErrorKind.BLOCKED, "url_malformed") from None
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_PORTS:
        raise FetchError(FetchErrorKind.BLOCKED, "scheme")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise FetchError(FetchErrorKind.BLOCKED, "credentials_in_url")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise FetchError(FetchErrorKind.BLOCKED, "no_host")
    if port is not None and port != ALLOWED_PORTS[scheme] and port not in extra_ports:
        raise FetchError(FetchErrorKind.BLOCKED, "port")
    try:
        # The name httpx will really connect to: its IDNA 2008 conversion differs from the
        # standard library's 2003 codec (for example the sharp s), so compare that spelling.
        host = httpx.URL(url).raw_host.decode("ascii").lower().rstrip(".")
    except (httpx.InvalidURL, UnicodeError):
        raise FetchError(FetchErrorKind.BLOCKED, "host_malformed") from None
    if host in blocked_hosts:
        raise FetchError(FetchErrorKind.BLOCKED, "own_host")
    return _Target(url=url, scheme=scheme, host=host)


class SafeHttpClient:
    def __init__(
        self,
        policy: AddressPolicy,
        *,
        resolver: Resolver | None = None,
        backend: httpcore.AsyncNetworkBackend | None = None,
        blocked_hosts: frozenset[str] = frozenset(),
        extra_ports: frozenset[int] = frozenset(),
        max_bytes: int = MAX_BYTES,
        deadline_seconds: float = DEADLINE_SECONDS,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        max_redirects: int = MAX_REDIRECTS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._blocked_hosts = blocked_hosts
        self._extra_ports = extra_ports
        self._max_bytes = max_bytes
        self._deadline = deadline_seconds
        self._max_redirects = max_redirects
        pinned = _PinnedBackend(
            backend or httpcore.AnyIOBackend(), resolver or SystemResolver(), policy
        )
        self._client = httpx.AsyncClient(
            transport=_Transport(pinned, httpx.create_ssl_context(verify=True)),
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(connect_timeout, read=connect_timeout, write=connect_timeout),
        )

    @classmethod
    def from_settings(cls, settings: "SettingsLike", **overrides: object) -> "SafeHttpClient":
        """The production way to build one: the policy comes from the settings, nowhere else."""
        policy = AddressPolicy(parse_networks(settings.fetch_test_allowed_networks_list))
        own = (urlsplit(settings.public_url).hostname or "").lower()
        return cls(
            policy,
            blocked_hosts=frozenset({own} if own else ()),
            extra_ports=frozenset(settings.fetch_test_allowed_ports_list),
            **overrides,  # type: ignore[arg-type]
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        bearer: Bearer | None = None,
    ) -> FetchResult:
        try:
            async with asyncio.timeout(self._deadline):
                return await self._follow(url, dict(headers or {}), bearer)
        except TimeoutError:
            raise FetchError(FetchErrorKind.TIMEOUT, "deadline") from None

    async def _follow(
        self, url: str, headers: dict[str, str], bearer: Bearer | None
    ) -> FetchResult:
        request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        for name, value in headers.items():
            if name.lower() not in CALLER_HEADERS:
                raise FetchError(FetchErrorKind.BLOCKED, "header_not_allowed")
            request_headers[name] = value
        request_headers["Accept-Encoding"] = "identity"

        current = url
        for _ in range(self._max_redirects + 1):
            target = check_url(current, self._blocked_hosts, self._extra_ports)
            hop_headers = dict(request_headers)
            if (
                bearer is not None
                and target.host == bearer.host.lower()
                and target.scheme == "https"
            ):
                hop_headers["Authorization"] = f"Bearer {bearer.token}"
            result = await self._once(target, hop_headers)
            if isinstance(result, FetchResult):
                return result
            current = urljoin(
                current, result
            )  # a redirect: the next URL, validated at the loop top
        raise FetchError(FetchErrorKind.BAD_STATUS, "too_many_redirects")

    def _until_rate_limit_reset(self, headers: Mapping[str, str]) -> int | None:
        """Seconds until a spent rate limit (GitHub: `x-ratelimit-remaining: 0`) is renewed."""
        reset = headers.get("x-ratelimit-reset", "")
        if headers.get("x-ratelimit-remaining") != "0" or not re.fullmatch(r"[0-9]{1,12}", reset):
            return None
        return max(int(reset) - int(self._clock()), 0) + 1

    async def _once(self, target: _Target, headers: dict[str, str]) -> "FetchResult | str":
        """One request. A FetchResult, or the Location to go to next."""
        try:
            async with self._client.stream("GET", target.url, headers=headers) as response:
                status = response.status_code
                if status in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError(FetchErrorKind.BAD_STATUS, "redirect_without_location")
                    return str(location)
                if status not in (200, 304):
                    retry = response.headers.get("retry-after", "")
                    seconds = int(retry) if re.fullmatch(r"[0-9]{1,7}", retry) else None
                    if seconds is None and status in (403, 429):
                        seconds = self._until_rate_limit_reset(response.headers)
                    raise FetchError(
                        FetchErrorKind.BAD_STATUS,
                        f"status_{status}",
                        retry_after=min(seconds, MAX_RETRY_AFTER) if seconds is not None else None,
                    )
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise FetchError(FetchErrorKind.BAD_CONTENT, "compressed")
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > self._max_bytes:
                    raise FetchError(FetchErrorKind.TOO_LARGE, "content_length")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        raise FetchError(FetchErrorKind.TOO_LARGE, "stream")
                kept = {k: response.headers[k] for k in RESPONSE_HEADERS if k in response.headers}
                return FetchResult(status, kept, bytes(body), target.url)
        except FetchError:
            raise
        except _Blocked as blocked:
            raise FetchError(FetchErrorKind.BLOCKED, blocked.reason) from None
        except httpx.InvalidURL:
            # httpx refuses some numeric spellings (0177.0.0.1) before any lookup happens.
            raise FetchError(FetchErrorKind.BLOCKED, "url_malformed") from None
        except httpx.TimeoutException:
            raise FetchError(FetchErrorKind.TIMEOUT, "socket_timeout") from None
        except (httpx.HTTPError, httpcore.ConnectError, ssl.SSLError, OSError) as error:
            # Only the class name is kept: messages may carry the address that was refused.
            logger.info("fetch failed", extra={"host": target.host, "error": type(error).__name__})
            raise FetchError(FetchErrorKind.NETWORK, type(error).__name__) from None


class SettingsLike(Protocol):
    public_url: str

    @property
    def fetch_test_allowed_networks_list(self) -> list[str]: ...

    @property
    def fetch_test_allowed_ports_list(self) -> list[int]: ...
