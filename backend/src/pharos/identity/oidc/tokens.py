"""Checking an ID token (OpenID Connect Core 3.1.3.7, RFC 8725).

What is accepted: a compact JWS signed with RS256, PS256 or ES256 by a key in the provider's key
set, whose `iss` is exactly the configured issuer, whose `aud` contains our client id (and whose
`azp`, if present or if there are several audiences, is our client id), that has not expired, was
not issued in the future (beyond a 60 second leeway), carries the `nonce` we sent, and has a usable
`sub`. With the Microsoft Entra ID preset the token must also come from the configured directory
(`tid`), be a version 2.0 token, and carry an object id (`oid`), which then identifies the person.
Everything else is refused, in particular `alg: none` and the HMAC algorithms (the classic
"sign with the public key as a secret" confusion): the algorithm list is fixed in our code and never
taken from the token.
"""

import hmac
import re
from dataclasses import dataclass
from typing import Any

from joserfc import jws, jwt
from joserfc.errors import (
    BadSignatureError,
    ExpiredTokenError,
    InvalidKeyIdError,
    JoseError,
    MissingKeyError,
    UnsupportedAlgorithmError,
)

from pharos.core.clock import Clock
from pharos.identity.oidc.client import IdpClient

ALLOWED_ALGORITHMS = ["RS256", "PS256", "ES256"]
LEEWAY_SECONDS = 60
MAX_TOKEN_BYTES = 16_384
MAX_SUBJECT = 256
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class TokenInvalid(Exception):
    """The ID token must not be trusted. `reason` is for the audit log, never for the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class IdentityClaims:
    issuer: str
    subject: str
    email: str | None
    display_name: str | None
    department: str | None
    raw: dict[str, Any]


def clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL.sub("", value).strip()
    return cleaned[:limit] or None


async def _decode(idp: IdpClient, token: str) -> jwt.Token:
    keys = await idp.key_set()
    try:
        return jwt.decode(token, keys, algorithms=ALLOWED_ALGORITHMS)
    except (InvalidKeyIdError, MissingKeyError):
        # An unknown key id: the provider may have rotated its keys. Look once more, then give up.
        keys = await idp.key_set(refresh=True)
        try:
            return jwt.decode(token, keys, algorithms=ALLOWED_ALGORITHMS)
        except (InvalidKeyIdError, MissingKeyError):
            raise TokenInvalid("unknown_key") from None
        except JoseError:
            raise TokenInvalid("bad_signature") from None
    except UnsupportedAlgorithmError:
        raise TokenInvalid("unsupported_algorithm") from None
    except BadSignatureError:
        raise TokenInvalid("bad_signature") from None
    except JoseError:
        raise TokenInvalid("malformed") from None


def _check_header(token: str) -> None:
    try:
        header = jws.extract_compact(token.encode()).headers()
    except (JoseError, ValueError):
        raise TokenInvalid("malformed") from None
    if header.get("alg") not in ALLOWED_ALGORITHMS:
        raise TokenInvalid("unsupported_algorithm")


async def validate_id_token(
    idp: IdpClient,
    token: str,
    *,
    nonce: str,
    clock: Clock,
    department_claim: str | None,
    entra_tenant: str | None = None,
    entra_allow_guests: bool = False,
) -> IdentityClaims:
    if len(token) > MAX_TOKEN_BYTES:
        raise TokenInvalid("too_large")
    _check_header(token)
    decoded = await _decode(idp, token)
    claims = decoded.claims
    now = int(clock.now().timestamp())

    registry = jwt.JWTClaimsRegistry(
        now=lambda: now,
        leeway=LEEWAY_SECONDS,
        iss={"essential": True, "value": idp.issuer},
        aud={"essential": True, "value": idp.client_id},
        exp={"essential": True},
        iat={"essential": True},
        sub={"essential": True},
    )
    if not isinstance(claims, dict):
        raise TokenInvalid("malformed")
    try:
        registry.validate(claims)
    except ExpiredTokenError:
        raise TokenInvalid("expired") from None
    except JoseError:
        raise TokenInvalid("bad_claims") from None

    audience = claims.get("aud")
    azp = claims.get("azp")
    if azp is not None and azp != idp.client_id:
        raise TokenInvalid("bad_authorized_party")
    if isinstance(audience, list) and len(audience) > 1 and azp != idp.client_id:
        raise TokenInvalid("bad_authorized_party")

    supplied = claims.get("nonce")
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), nonce.encode()):
        raise TokenInvalid("bad_nonce")

    subject = claims.get("sub")
    if entra_tenant is not None:
        # Microsoft Entra ID: the issuer already names the directory, and `tid` must agree. The
        # person is the object id (`oid`), the same in every app of the directory; `sub` differs
        # per app. Together with the issuer that is the pair (tid, oid).
        tenant = claims.get("tid")
        if not isinstance(tenant, str) or tenant.lower() != entra_tenant:
            raise TokenInvalid("bad_tenant")
        if claims.get("ver") != "2.0":
            raise TokenInvalid("bad_version")
        # A guest (B2B) comes from another directory but is issued a token by ours. When the
        # optional claims say so (`acct` 1, or an `idp` that is not this directory), refuse by
        # default; assigning people to the application in Entra is the stronger control.
        identity_provider = claims.get("idp")
        if not entra_allow_guests and (
            claims.get("acct") == 1
            or (identity_provider is not None and identity_provider != idp.issuer)
        ):
            raise TokenInvalid("guest_not_allowed")
        object_id = claims.get("oid")
        if not isinstance(object_id, str) or not _GUID.fullmatch(object_id.lower()):
            raise TokenInvalid("bad_subject")
        subject = object_id.lower()
    if (
        not isinstance(subject, str)
        or not subject
        or len(subject) > MAX_SUBJECT
        or _CONTROL.search(subject)
    ):
        raise TokenInvalid("bad_subject")

    email = clean_text(claims.get("email"), 320)
    # `email_verified` is deliberately ignored: the e-mail is information, never an identity key.
    display_name = (
        clean_text(claims.get("name"), 200)
        or clean_text(claims.get("preferred_username"), 200)
        or (email[:200] if email else None)
    )
    department = clean_text(claims.get(department_claim), 200) if department_claim else None
    return IdentityClaims(
        issuer=idp.issuer,
        subject=subject,
        email=email,
        display_name=display_name,
        department=department,
        raw=dict(claims),
    )


__all__ = [
    "ALLOWED_ALGORITHMS",
    "IdentityClaims",
    "TokenInvalid",
    "clean_text",
    "validate_id_token",
]
