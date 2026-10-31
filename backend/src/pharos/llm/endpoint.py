"""A look at where a configured endpoint really points (ADR-0015 point 10).

The settings refuse an endpoint that is not a public DNS name. That is a check of spelling; a name
can still resolve to an address inside the network. Before the first call to an endpoint, and
again after a minute, its name is resolved and every address must pass the same policy that
`SafeHttpClient` applies to feeds. What remains is the gap between this look and LiteLLM's own
look-up a moment later (a name that changes in between); the endpoint is chosen by an
administrator, not by an article, which is why that gap is accepted and written down.
"""

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlsplit

from pharos.llm.errors import ProviderRejected
from pharos.net.addresses import AddressPolicy

CACHE_SECONDS = 60.0
LOOKUP_SECONDS = 5.0


class EndpointCheck:
    def __init__(self, *, policy: AddressPolicy | None = None) -> None:
        self._policy = policy or AddressPolicy()
        self._checked: dict[str, float] = {}

    async def ensure_public(self, base_url: str) -> None:
        host = urlsplit(base_url).hostname or ""
        if not host:
            raise ProviderRejected()
        now = time.monotonic()
        if self._checked.get(host, -CACHE_SECONDS) + CACHE_SECONDS > now:
            return
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(LOOKUP_SECONDS):
                infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except (OSError, TimeoutError):
            raise ProviderRejected() from None
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        if self._policy.violations(addresses) is not None:
            raise ProviderRejected()
        self._checked[host] = now
