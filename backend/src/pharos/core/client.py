"""The address of the client making a request."""

from starlette.requests import Request


def client_ip(request: Request) -> str | None:
    """The peer address as the server sees it.

    uvicorn replaces it with the forwarded address only when the connection comes from the trusted
    proxy (`--forwarded-allow-ips`, pinned to Caddy). This function must never read
    `X-Forwarded-For`
    itself: any client could send that header.
    """
    return request.client.host if request.client else None
