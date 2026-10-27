"""A fake OpenID Connect provider for tests: discovery, keys, authorization codes, a token endpoint.

It checks what a real provider checks (client authentication, redirect URI, single-use codes,
PKCE S256) so the tests exercise our side of the protocol, and it has switches for every way a
provider or an attacker can misbehave. Keys are generated per instance. Nothing here talks to the
network.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlsplit

import httpx
from joserfc import jwt
from joserfc.jwk import ECKey, KeySet, RSAKey

ISSUER = "https://idp.test/realms/pharos"
CLIENT_ID = "pharos-web"
CLIENT_SECRET = "client-secret-for-tests-only"
REDIRECT_URI = "https://pharos.example/api/auth/oidc/callback"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def compact(header: dict[str, Any], claims: dict[str, Any], signature: bytes = b"") -> str:
    """Assemble a compact JWS by hand, for tokens a library would refuse to make."""
    head = b64(json.dumps(header).encode())
    body = b64(json.dumps(claims).encode())
    return f"{head}.{body}.{b64(signature)}"


def reply(
    status: int = 200, *, json_: Any = None, content: bytes | None = None, headers: Any = None
) -> httpx.Response:
    """A response with a real byte stream, as it arrives from the network (not pre-read)."""
    body = json.dumps(json_).encode() if json_ is not None else (content or b"")
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


class FakeIdp:
    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self.now = now
        self.issuer = ISSUER
        self.client_id = CLIENT_ID
        self.client_secret = CLIENT_SECRET
        self.rsa = RSAKey.generate_key(2048, parameters={"kid": "rsa-1", "use": "sig"})
        self.ec = ECKey.generate_key("P-256", parameters={"kid": "ec-1", "use": "sig"})
        self.published: list[Any] = [self.rsa, self.ec]
        self.codes: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        # Switches.
        self.discovery: dict[str, Any] = {}
        self.discovery_status = 200
        self.token_status = 200
        self.token_body: bytes | None = None
        self.token_extra: dict[str, Any] = {}
        self.redirect_to: str | None = None
        self.raise_on: dict[str, Exception] = {}
        self.oversize = False
        self.gzip_bomb = False
        self.require_basic_auth = True
        # Microsoft Graph (only the Entra preset calls it).
        self.graph_status = 200
        self.graph_body: Any = None  # replaces the whole answer when set
        self.graph_department: str | None = "Finance"
        self.graph_requests: list[tuple[str, str | None]] = []  # (URL, Authorization header)

    # --- documents ----------------------------------------------------------------------------

    def discovery_document(self) -> dict[str, Any]:
        base = self.issuer
        document: dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{base}/protocol/openid-connect/auth",
            "token_endpoint": f"{base}/protocol/openid-connect/token",
            "jwks_uri": f"{base}/protocol/openid-connect/certs",
            "end_session_endpoint": f"{base}/protocol/openid-connect/logout",
            "code_challenge_methods_supported": ["S256", "plain"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        }
        document.update(self.discovery)
        return {k: v for k, v in document.items() if v is not None}

    def jwks(self) -> dict[str, Any]:
        return KeySet(self.published).as_dict(private=False)  # type: ignore[no-any-return]

    def rotate(self) -> None:
        """The provider starts signing with a new key and publishes it (the old one stays)."""
        self.rsa = RSAKey.generate_key(2048, parameters={"kid": "rsa-2", "use": "sig"})
        self.published = [self.rsa, self.ec]

    # --- tokens -------------------------------------------------------------------------------

    def claims(self, **overrides: Any) -> dict[str, Any]:
        now = int(self.now())
        base: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": "subject-1",
            "iat": now,
            "exp": now + 300,
            "nonce": "nonce",
            "email": "user@example.test",
            "name": "Test User",
        }
        base.update(overrides)
        return {k: v for k, v in base.items() if v is not None}

    def sign(self, claims: dict[str, Any], *, alg: str = "RS256", key: Any = None) -> str:
        key = key or (self.ec if alg == "ES256" else self.rsa)
        return jwt.encode({"alg": alg, "kid": key.kid}, claims, key)  # type: ignore[no-any-return]

    def none_token(self, claims: dict[str, Any]) -> str:
        return compact({"alg": "none", "typ": "JWT"}, claims)

    def hs256_token(self, claims: dict[str, Any]) -> str:
        """The classic confusion: an HMAC token keyed with the provider's PUBLIC key."""
        public_pem = self.rsa.as_pem(private=False)
        signing_input = compact({"alg": "HS256", "kid": self.rsa.kid}, claims).rsplit(".", 1)[0]
        signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
        return f"{signing_input}.{b64(signature)}"

    # --- the authorization step (what happens at the provider after the person logs in) --------

    def authorize(self, authorization_url: str, **claims: Any) -> tuple[str, dict[str, str]]:
        """Pretend the person signed in. Returns a code and the callback query parameters."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorization_url).query).items()}
        assert query["client_id"] == self.client_id
        assert query["redirect_uri"] == REDIRECT_URI
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(24)
        self.codes[code] = {
            "challenge": query["code_challenge"],
            "nonce": query["nonce"],
            "claims": claims,
            "used": False,
        }
        return code, {"code": code, "state": query["state"]}

    # --- the HTTP side ------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "graph.microsoft.com":
            return self._graph(request)
        self.requests.append((request.method, str(request.url)))
        for suffix, error in self.raise_on.items():
            if path.endswith(suffix):
                raise error
        if path.endswith("/.well-known/openid-configuration"):
            if self.redirect_to:
                return reply(302, headers={"Location": self.redirect_to})
            if self.gzip_bomb:
                import gzip

                packed = gzip.compress(b"{" + b" " * 50_000_000 + b"}")
                return reply(200, content=packed, headers={"Content-Encoding": "gzip"})
            if self.oversize:
                return reply(200, content=b"{" + b" " * 2_000_000 + b"}")
            if self.discovery_status != 200:
                return reply(self.discovery_status, json_={"error": "x"})
            return reply(200, json_=self.discovery_document())
        if path.endswith(("/certs", "/keys")):
            return reply(200, json_=self.jwks())
        if path.endswith("/token"):
            return self._token(request)
        return reply(404)

    def _graph(self, request: httpx.Request) -> httpx.Response:
        self.graph_requests.append((str(request.url), request.headers.get("authorization")))
        if request.headers.get("authorization") != "Bearer opaque-access-token":
            return reply(401, json_={"error": {"code": "InvalidAuthenticationToken"}})
        if self.graph_status != 200:
            return reply(self.graph_status, json_={"error": {"code": "Failure"}})
        if self.graph_body is not None:
            return reply(200, json_=self.graph_body)
        body: dict[str, Any] = {
            "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users(department)/$entity"
        }
        if self.graph_department is not None:
            body["department"] = self.graph_department
        return reply(200, json_=body)

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.token_status != 200:
            return reply(self.token_status, json_={"error": "invalid_grant"})
        if self.token_body is not None:
            return reply(200, content=self.token_body)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if self.require_basic_auth:
            # RFC 6749 section 2.3.1: id and secret are form-encoded before they are joined.
            pair = f"{quote_plus(self.client_id)}:{quote_plus(self.client_secret)}"
            expected = base64.b64encode(pair.encode()).decode()
            if request.headers.get("authorization") != f"Basic {expected}":
                return reply(401, json_={"error": "invalid_client"})
        record = self.codes.get(form.get("code", ""))
        if record is None or record["used"] or form.get("redirect_uri") != REDIRECT_URI:
            return reply(400, json_={"error": "invalid_grant"})
        record["used"] = True
        verifier = form.get("code_verifier", "")
        challenge = b64(hashlib.sha256(verifier.encode()).digest())
        if not hmac.compare_digest(challenge, record["challenge"]):
            return reply(400, json_={"error": "invalid_grant"})  # PKCE failed
        claims = self.claims(**{"nonce": record["nonce"], **record["claims"]})
        body = {"id_token": self.sign(claims), "access_token": "opaque-access-token"}
        body.update(self.token_extra)
        return reply(200, json_=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


ENTRA_TENANT = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
ENTRA_ISSUER = f"https://login.microsoftonline.com/{ENTRA_TENANT}/v2.0"
ENTRA_OBJECT_ID = "11111111-2222-4333-8444-555555555555"


def entra_idp(now: Callable[[], float] = time.time, tenant: str = ENTRA_TENANT) -> FakeIdp:
    """A provider shaped like Microsoft Entra ID's v2.0 endpoint (made-up tenant and object ids)."""
    idp = FakeIdp(now)
    base = f"https://login.microsoftonline.com/{tenant}"
    idp.issuer = f"{base}/v2.0"
    idp.discovery = {
        "authorization_endpoint": f"{base}/oauth2/v2.0/authorize",
        "token_endpoint": f"{base}/oauth2/v2.0/token",
        "jwks_uri": f"{base}/discovery/v2.0/keys",
        "end_session_endpoint": f"{base}/oauth2/v2.0/logout",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "private_key_jwt",
            "client_secret_basic",
        ],
    }
    return idp


def entra_claims(_provider: FakeIdp, **overrides: Any) -> dict[str, Any]:
    """What an Entra ID v2.0 ID token carries (`sub` is per application, `oid` is the person)."""
    base: dict[str, Any] = {
        "tid": ENTRA_TENANT,
        "oid": ENTRA_OBJECT_ID,
        "ver": "2.0",
        "sub": "AAAAAAAAAAAAAAAAAAAAAIkzqFVrSaSaFHy782bbtaQ",
        "preferred_username": "user@example.test",
        "email": None,
    }
    base.update(overrides)
    return base
