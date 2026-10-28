"""The only module that talks to an identity provider (ADR-0011).

It is the one deliberate exception to "outbound HTTP only through SafeHttpClient": that client keeps
untrusted public sources out of private networks, while the identity provider is a configured,
trusted endpoint that may well live on a private address (a corporate Keycloak). Instead of address
filtering this client is strict about WHAT it calls and HOW:

* It reads the provider's discovery document, requires its `issuer` to equal the configured one, and
  afterwards calls only the endpoints named there (authorization, token, keys, end-session).
* https is required in production (plain http only in development and tests).
* Redirects are never followed, responses are size-limited, every call has a timeout.
* A signing key that is not in the cached key set causes one refetch, and refetching is rate
  limited, so a stream of bogus tokens cannot turn the API into a request amplifier.
* The client secret and tokens are never logged; error messages carry no response bodies.

`httpx` is imported nowhere else (an import-linter contract enforces it).
"""

import asyncio
import json
import logging
import os
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus, urlsplit

import httpx
from joserfc.jwk import KeySet

from pharos.core.clock import Clock

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 1_048_576
DISCOVERY_TTL_SECONDS = 3600
JWKS_REFETCH_MIN_INTERVAL_SECONDS = 60
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# The department is a nicety: do not hold a sign-in (and its database connection) for long.
GRAPH_TIMEOUT = httpx.Timeout(4.0, connect=2.0)
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me?$select=department,userType"


class IdpUnavailable(Exception):
    """The provider could not be reached or answered something unusable."""


class IdpRejected(Exception):
    """The provider refused the request (for example an invalid or reused authorization code)."""


@dataclass(frozen=True, slots=True)
class TokenSet:
    # Neither token appears in a repr: an error report showing local variables must not leak them.
    id_token: str = field(repr=False)
    access_token: str | None = field(repr=False)  # for Microsoft Graph; never stored


@dataclass(frozen=True, slots=True)
class GraphProfile:
    department: str | None
    user_type: str | None  # "Member" or "Guest" in Entra ID


@dataclass(frozen=True, slots=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None
    token_auth_methods: tuple[str, ...]
    # RFC 9207: the provider says it adds `iss` to authorization responses, so we require it.
    iss_parameter_required: bool


def _check_endpoint(name: str, url: object, *, allow_http: bool) -> str:
    if not isinstance(url, str):
        raise IdpUnavailable(f"discovery: {name} is missing")
    parts = urlsplit(url)
    allowed = ("https", "http") if allow_http else ("https",)
    if parts.scheme not in allowed or not parts.hostname or parts.username or parts.password:
        raise IdpUnavailable(f"discovery: {name} is not an acceptable URL")
    if parts.fragment:
        raise IdpUnavailable(f"discovery: {name} has a fragment")
    return url


def parse_discovery(document: dict[str, Any], issuer: str, *, allow_http: bool) -> Discovery:
    if document.get("issuer") != issuer:
        # The most important check: a provider that names another issuer is a different provider.
        raise IdpUnavailable("discovery: the issuer in the document is not the configured one")
    methods = document.get("code_challenge_methods_supported")
    if methods is not None and "S256" not in methods:
        raise IdpUnavailable("discovery: the provider does not support PKCE with S256")
    end_session = document.get("end_session_endpoint")
    auth_methods = document.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    return Discovery(
        issuer=issuer,
        authorization_endpoint=_check_endpoint(
            "authorization_endpoint", document.get("authorization_endpoint"), allow_http=allow_http
        ),
        token_endpoint=_check_endpoint(
            "token_endpoint", document.get("token_endpoint"), allow_http=allow_http
        ),
        jwks_uri=_check_endpoint("jwks_uri", document.get("jwks_uri"), allow_http=allow_http),
        end_session_endpoint=(
            _check_endpoint("end_session_endpoint", end_session, allow_http=allow_http)
            if end_session
            else None
        ),
        token_auth_methods=tuple(str(m) for m in auth_methods),
        iss_parameter_required=(
            document.get("authorization_response_iss_parameter_supported") is True
        ),
    )


class IdpClient:
    def __init__(
        self,
        http: httpx.AsyncClient | None,
        clock: Clock,
        *,
        issuer: str,
        client_id: str,
        client_secret: str | None,
        allow_http: bool,
    ) -> None:
        # Tests give a client wired to a fake provider. Otherwise the client is made on first use,
        # not at start-up: building it reads the trusted certificates, and a provider that is
        # unreachable or not yet trusted must not stop the API from starting.
        self._given_http = http
        self._own_http: httpx.AsyncClient | None = None
        self._clock = clock
        self.issuer = issuer
        self.client_id = client_id
        self._client_secret = client_secret
        self._allow_http = allow_http
        self._discovery: Discovery | None = None
        self._discovery_at = 0.0
        self._keys: KeySet | None = None
        self._keys_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._given_http is not None:
            return self._given_http
        if self._own_http is None:
            try:
                # trust_env=False: proxy variables and .netrc must not redirect provider traffic
                # (it carries the client secret) outside the documented trust boundary. The
                # certificate variables are still honoured, on purpose: the development stack
                # trusts its own CA through them.
                context = ssl.create_default_context(
                    cafile=os.environ.get("SSL_CERT_FILE") or None,
                    capath=os.environ.get("SSL_CERT_DIR") or None,
                )
                self._own_http = httpx.AsyncClient(trust_env=False, verify=context)
            except (OSError, ssl.SSLError):
                logger.warning("identity provider client: the trusted certificates are unreadable")
                raise IdpUnavailable("the trusted certificates could not be loaded") from None
        return self._own_http

    @_http.setter
    def _http(self, client: httpx.AsyncClient) -> None:  # tests swap in a fake provider
        self._given_http = client

    async def aclose(self) -> None:
        if self._own_http is not None:
            await self._own_http.aclose()

    # --- plumbing -----------------------------------------------------------------------------

    async def _fetch_json(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        auth: Any = None,
        bearer: str | None = None,
        time_limit: httpx.Timeout = TIMEOUT,
    ) -> dict[str, Any]:
        try:
            async with self._http.stream(
                method,
                url,
                data=data,
                auth=auth,
                # No compression: a small compressed answer could inflate far past the size limit
                # before it is counted.
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    **({"Authorization": f"Bearer {bearer}"} if bearer else {}),
                },
                follow_redirects=False,
                timeout=time_limit,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise IdpUnavailable("the provider answered with a redirect")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise IdpUnavailable("the provider's answer is too large")
                status = response.status_code
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as error:
            # Only the class goes into the log: httpx messages can contain URLs with parameters.
            logger.warning(
                "identity provider request failed", extra={"error": type(error).__name__}
            )
            raise IdpUnavailable("the provider could not be reached") from None
        if 400 <= status < 500:
            raise IdpRejected(f"the provider answered {status}")
        if status >= 500:
            raise IdpUnavailable(f"the provider answered {status}")
        try:
            parsed = json.loads(bytes(body))
        except ValueError:
            raise IdpUnavailable("the provider's answer is not JSON") from None
        if not isinstance(parsed, dict):
            raise IdpUnavailable("the provider's answer is not a JSON object")
        return parsed

    # --- discovery and keys -------------------------------------------------------------------

    async def discovery(self) -> Discovery:
        now = self._clock.now().timestamp()
        if self._discovery is not None and now - self._discovery_at < DISCOVERY_TTL_SECONDS:
            return self._discovery
        async with self._lock:
            if self._discovery is not None and now - self._discovery_at < DISCOVERY_TTL_SECONDS:
                return self._discovery  # another caller fetched it while this one waited
            url = self.issuer.rstrip("/") + "/.well-known/openid-configuration"
            try:
                document = await self._fetch_json("GET", url)
            except IdpRejected as error:
                raise IdpUnavailable("discovery: the provider refused the request") from error
            self._discovery = parse_discovery(document, self.issuer, allow_http=self._allow_http)
            self._discovery_at = now
            return self._discovery

    async def key_set(self, *, refresh: bool = False) -> KeySet:
        """The provider's signing keys. `refresh` asks for a new copy, at most once a minute."""
        now = self._clock.now().timestamp()
        if self._keys is not None and not (
            refresh and now - self._keys_at >= JWKS_REFETCH_MIN_INTERVAL_SECONDS
        ):
            return self._keys
        discovery = await self.discovery()
        async with self._lock:
            if self._keys is not None and not (
                refresh and now - self._keys_at >= JWKS_REFETCH_MIN_INTERVAL_SECONDS
            ):
                return self._keys  # another caller refreshed them while this one waited
            try:
                document = await self._fetch_json("GET", discovery.jwks_uri)
                self._keys = KeySet.import_key_set(document)  # type: ignore[arg-type]
            except IdpRejected as error:
                raise IdpUnavailable("the provider refused to give its keys") from error
            except Exception as error:  # a malformed key set must not be taken for a success
                if isinstance(error, IdpUnavailable):
                    raise
                raise IdpUnavailable("the provider's key set is not usable") from None
            self._keys_at = now
            return self._keys

    # --- the code exchange --------------------------------------------------------------------

    async def exchange_code_tokens(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> "TokenSet":
        """Trade the authorization code for the tokens."""
        discovery = await self.discovery()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        auth: httpx.BasicAuth | None = None
        if self._client_secret is None:
            form["client_id"] = self.client_id
        elif "client_secret_basic" in discovery.token_auth_methods:
            # RFC 6749 section 2.3.1: both parts are form-encoded before they are joined.
            auth = httpx.BasicAuth(quote_plus(self.client_id), quote_plus(self._client_secret))
        else:
            form["client_id"] = self.client_id
            form["client_secret"] = self._client_secret
        response = await self._fetch_json("POST", discovery.token_endpoint, data=form, auth=auth)
        id_token = response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise IdpUnavailable("the token response has no ID token")
        access_token = response.get("access_token")
        return TokenSet(id_token, access_token if isinstance(access_token, str) else None)

    async def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> str:
        """Like `exchange_code_tokens`, but returns only the ID token."""
        return (await self.exchange_code_tokens(code, code_verifier, redirect_uri)).id_token

    async def graph_profile(self, access_token: str) -> "GraphProfile":
        """Microsoft Graph: department and user type of the person who just signed in (Entra only).

        The address is a constant of ours, never taken from a token or a discovery document. The
        access token is the provider's, issued for Graph, and is not validated here: it is only
        handed back to Graph, which does that itself. Any failure means "nothing known".
        """
        try:
            profile = await self._fetch_json(
                "GET", GRAPH_ME_URL, bearer=access_token, time_limit=GRAPH_TIMEOUT
            )
        except (IdpUnavailable, IdpRejected):
            return GraphProfile(None, None)
        department = profile.get("department")
        user_type = profile.get("userType")
        return GraphProfile(
            department if isinstance(department, str) else None,
            user_type if isinstance(user_type, str) else None,
        )

    async def graph_department(self, access_token: str) -> str | None:
        return (await self.graph_profile(access_token)).department
