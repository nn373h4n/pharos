"""The short-lived cookie that ties an authorization request to the browser that started it.

Without it an attacker could start a sign-in, give the callback link (with the attacker's `state`
and `code`) to a victim and sign the victim in as the attacker ("login CSRF"). With it, the callback
only works in the browser that also holds the cookie value that went into the key of the stored
state.
"""

from fastapi import Request
from starlette.responses import Response

BINDING_COOKIE = "pharos_oidc"
BINDING_MAX_AGE_SECONDS = 600


class BindingCookie:
    def __init__(self, public_url: str) -> None:
        self.secure = public_url.startswith("https://")
        self.name = f"__Host-{BINDING_COOKIE}" if self.secure else BINDING_COOKIE

    def read(self, request: Request) -> str | None:
        return request.cookies.get(self.name)

    def set(self, response: Response, value: str) -> None:
        # SameSite=Lax still travels with the top-level navigation that brings the person back
        # from the provider; it is withheld from cross-site sub-requests.
        response.set_cookie(
            self.name,
            value,
            max_age=BINDING_MAX_AGE_SECONDS,
            secure=self.secure,
            httponly=True,
            samesite="lax",
            path="/",
        )

    def clear(self, response: Response) -> None:
        response.delete_cookie(
            self.name, secure=self.secure, httponly=True, samesite="lax", path="/"
        )
