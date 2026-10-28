"""Single sign-in with any OpenID Connect provider (authorization code flow with PKCE).

The steps, and what each defends against (ADR-0011):

* `begin` creates `state`, `nonce` and a PKCE verifier, stores them in Redis under a key that also
  contains a random value given to this browser only (the binding cookie), and sends the person to
  the provider. The verifier and the target page never leave the server.
* `finish` consumes the stored record with GETDEL, so a callback works once and only in the browser
  that started it (state, replay and login-CSRF checks). It then checks the optional RFC 9207 `iss`
  parameter, exchanges the code, validates the ID token and finds or creates the user by
  (organization, issuer, subject), never by e-mail.
* A disabled user is never signed in and never re-activated. A new user is a plain member; roles
  come from administrators, not from the provider (group mapping needs its own design).
* Every failure is one short code for the browser; the precise reason goes to the audit log.
"""

import base64
import hashlib
import json
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from redis.asyncio import Redis
from redis.exceptions import RedisError

from pharos.core.audit import ActorType, AuditAction, AuditLog, Outcome
from pharos.core.clock import Clock
from pharos.core.errors import DependencyUnavailable
from pharos.core.ratelimit import RateLimiter
from pharos.core.sessions import SessionRecord, SessionStore
from pharos.core.unit_of_work import UnitOfWork
from pharos.identity import repository
from pharos.identity.oidc.client import IdpClient, IdpRejected, IdpUnavailable
from pharos.identity.oidc.tokens import TokenInvalid, validate_id_token

logger = logging.getLogger(__name__)

STATE_TTL_SECONDS = 600
STATE_PREFIX = "pharos:oidc:"
CALLBACK_PATH = "/api/auth/oidc/callback"
MAX_PARAMETER = 2048
# Per source address. People behind one corporate egress address share it, so it is generous; it
# only has to stop a loop, not a crowd (see THREAT_MODEL).
ATTEMPTS_PER_MINUTE = 120
# Refusals anyone can cause without having started a flow. They are logged, not audited row by row.
_UNAUDITED = frozenset({"state_missing", "state_mismatch", "state_corrupt", "too_many_attempts"})
# What a provider may put in `error` (RFC 6749 section 4.1.2.1 and OpenID Connect Core 3.1.2.6).
_PROVIDER_ERRORS = frozenset(
    {
        "invalid_request",
        "unauthorized_client",
        "access_denied",
        "unsupported_response_type",
        "invalid_scope",
        "server_error",
        "temporarily_unavailable",
        "interaction_required",
        "login_required",
        "account_selection_required",
        "consent_required",
    }
)
MAX_NEXT = 512

# What the browser is told. Never the reason.
FAILED = "sso_failed"
DENIED = "sso_denied"
UNAVAILABLE = "sso_unavailable"
DISABLED = "account_disabled"
BUSY = "rate_limited"


class OidcFailure(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def safe_next(value: str | None) -> str:
    """Where to go after sign-in: a path on this site, nothing else.

    A scheme, a host, `//host`, a backslash (browsers treat it as a slash) and control characters
    are all refused, so the sign-in endpoint cannot be used as an open redirect.
    """
    if not value or len(value) > MAX_NEXT or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return "/"
    if value.startswith("/api/"):
        return "/"  # API paths return JSON, not pages
    return value


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _key(state: str, binding: str) -> str:
    return STATE_PREFIX + hashlib.sha256(f"{state}:{binding}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Started:
    url: str
    binding: str


@dataclass(frozen=True, slots=True)
class SignedIn:
    session_id: str
    record: SessionRecord
    next_path: str


class OidcService:
    def __init__(
        self,
        *,
        idp: IdpClient,
        redis: Redis,
        clock: Clock,
        audit: AuditLog,
        sessions: SessionStore,
        limiter: RateLimiter,
        public_url: str,
        scopes: str,
        provider_name: str,
        department_claim: str | None,
    ) -> None:
        self.idp = idp
        self.provider_name = provider_name
        self._redis = redis
        self._clock = clock
        self._audit = audit
        self._sessions = sessions
        self._limiter = limiter
        self._public_url = public_url
        self._scopes = scopes
        self._department_claim = department_claim

    @property
    def redirect_uri(self) -> str:
        return self._public_url + CALLBACK_PATH

    async def _throttle(self, kind: str, ip: str | None) -> None:
        decision = await self._limiter.hit(
            f"oidc:{kind}:{ip or 'unknown'}", limit=ATTEMPTS_PER_MINUTE, window_seconds=60
        )
        if not decision.allowed:
            raise OidcFailure(BUSY, "too_many_attempts")

    # --- step 1: send the person to the provider ----------------------------------------------

    async def begin(self, *, ip: str | None, next_path: str | None) -> Started:
        await self._throttle("begin", ip)
        discovery = await self._discovery()
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier, binding = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        record = json.dumps({"nonce": nonce, "verifier": verifier, "next": safe_next(next_path)})
        try:
            await self._redis.set(_key(state, binding), record, ex=STATE_TTL_SECONDS)
        except RedisError as error:
            raise DependencyUnavailable from error
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.idp.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self._scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "response_mode": "query",
            }
        )
        separator = "&" if "?" in discovery.authorization_endpoint else "?"
        return Started(discovery.authorization_endpoint + separator + query, binding)

    async def _discovery(self) -> Any:
        try:
            return await self.idp.discovery()
        except (IdpUnavailable, IdpRejected) as error:
            raise OidcFailure(UNAVAILABLE, "discovery_failed") from error

    # --- step 2: the person comes back ---------------------------------------------------------

    async def finish(
        self,
        uow: UnitOfWork,
        *,
        params: Mapping[str, str],
        binding: str | None,
        ip: str | None,
        previous_session_id: str | None,
    ) -> SignedIn:
        # Throttled before anything is written: a flood of bogus callbacks must not also become a
        # flood of audit rows.
        await self._throttle("callback", ip)
        try:
            return await self._finish(uow, params, binding, ip, previous_session_id)
        except OidcFailure as failure:
            if failure.reason in _UNAUDITED:
                # Anyone can send these without having started a flow; they are counted by the
                # throttle and visible in the request log, not stored one by one.
                logger.info("single sign-in refused", extra={"reason": failure.reason})
                raise
            await self._audit.record_independently(
                org_id=uow.org_id,
                action=AuditAction.LOGIN,
                outcome=Outcome.FAILURE,
                actor_type=ActorType.SYSTEM,
                target="oidc",
                ip=ip,
                details={"method": "oidc", "reason": failure.reason},
            )
            raise

    async def _take_state(self, state: str, binding: str) -> dict[str, Any]:
        try:
            raw = await self._redis.getdel(_key(state, binding))
        except RedisError as error:
            raise DependencyUnavailable from error
        if raw is None:
            # Unknown, expired, already used, or started in another browser: indistinguishable.
            raise OidcFailure(FAILED, "state_mismatch")
        try:
            record = json.loads(raw)
            return {k: str(record[k]) for k in ("nonce", "verifier", "next")}
        except (ValueError, KeyError, TypeError):
            raise OidcFailure(FAILED, "state_corrupt") from None

    async def _finish(
        self,
        uow: UnitOfWork,
        params: Mapping[str, str],
        binding: str | None,
        ip: str | None,
        previous_session_id: str | None,
    ) -> SignedIn:
        state, code = params.get("state"), params.get("code")
        if not state or len(state) > MAX_PARAMETER or not binding or len(binding) > MAX_PARAMETER:
            raise OidcFailure(FAILED, "state_missing")
        stored = await self._take_state(state, binding)  # consumed even if the provider says no

        error = params.get("error")
        if error:
            if error == "access_denied":
                raise OidcFailure(DENIED, "provider_denied")
            known = error if error in _PROVIDER_ERRORS else "other"
            raise OidcFailure(FAILED, f"provider_error:{known}")
        if not code or len(code) > MAX_PARAMETER:
            raise OidcFailure(FAILED, "code_missing")
        discovery = await self._discovery()
        if "iss" in params:
            if params["iss"] != self.idp.issuer:
                raise OidcFailure(FAILED, "issuer_mismatch")  # RFC 9207: a mix-up attack
        elif discovery.iss_parameter_required:
            raise OidcFailure(FAILED, "issuer_parameter_missing")

        try:
            id_token = await self.idp.exchange_code(code, stored["verifier"], self.redirect_uri)
        except IdpRejected as error_:
            raise OidcFailure(FAILED, "code_rejected") from error_
        except IdpUnavailable as error_:
            raise OidcFailure(UNAVAILABLE, "token_endpoint_unavailable") from error_
        try:
            claims = await validate_id_token(
                self.idp,
                id_token,
                nonce=stored["nonce"],
                clock=self._clock,
                department_claim=self._department_claim,
            )
        except TokenInvalid as invalid:
            raise OidcFailure(FAILED, f"token:{invalid.reason}") from invalid
        except IdpUnavailable as error_:
            raise OidcFailure(UNAVAILABLE, "keys_unavailable") from error_

        user = await repository.upsert_oidc_user(
            uow.session,
            uow.org_id,
            issuer=claims.issuer,
            subject=claims.subject,
            email=claims.email,
            display_name=claims.display_name,
        )
        if user.status != "active":
            raise OidcFailure(DISABLED, "inactive_user")
        target = f"user:{user.id}"
        if user.created:
            await self._audit.record(
                uow.session,
                org_id=uow.org_id,
                action=AuditAction.USER_CREATED,
                actor_type=ActorType.SYSTEM,
                target=f"user:{user.id}",
                ip=ip,
                details={"method": "oidc"},
            )
        if claims.department:
            await repository.apply_idp_department(
                uow.session, uow.org_id, user.id, claims.department
            )
        await repository.touch_last_login(uow.session, uow.org_id, user.id)
        await self._audit.record(
            uow.session,
            org_id=uow.org_id,
            action=AuditAction.LOGIN,
            actor_type=ActorType.USER,
            actor_id=user.id,
            target=target,
            ip=ip,
            details={"method": "oidc"},
        )
        await uow.commit()  # the database first, then Redis (ADR-0011)

        session_id, record = await self._sessions.create(user.id, uow.org_id, method="oidc")
        if previous_session_id:
            await self._sessions.delete(previous_session_id)
        return SignedIn(session_id, record, stored["next"])

    # --- sign-out at the provider ---------------------------------------------------------------

    async def end_session_url(self) -> str | None:
        """Where to send the person to end the provider's own session too (RP-initiated logout)."""
        try:
            discovery = await self.idp.discovery()
        except (IdpUnavailable, IdpRejected):
            return None
        if not discovery.end_session_endpoint:
            return None
        query = urlencode(
            {
                "client_id": self.idp.client_id,
                "post_logout_redirect_uri": self._public_url + "/login",
            }
        )
        separator = "&" if "?" in discovery.end_session_endpoint else "?"
        return discovery.end_session_endpoint + separator + query
