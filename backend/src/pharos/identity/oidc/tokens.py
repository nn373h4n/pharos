"""Checking an ID token (OpenID Connect Core 3.1.3.7, RFC 8725).

What is accepted: a compact JWS signed with RS256, PS256 or ES256 by a key in the provider's key
set, whose `iss` is exactly the configured issuer, whose `aud` contains our client id (and whose
`azp`, if present or if there are several audiences, is our client id), that has not expired, was
not issued in the future (beyond a 60 second leeway), carries the `nonce` we sent, and has a usable
`sub`. Everything else is refused, in particular `alg: none` and the HMAC algorithms (the classic
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


def _text(value: object, limit: int) -> str | None:
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
    if (
        not isinstance(subject, str)
        or not subject
        or len(subject) > MAX_SUBJECT
        or _CONTROL.search(subject)
    ):
        raise TokenInvalid("bad_subject")

    email = _text(claims.get("email"), 320)
    # `email_verified` is deliberately ignored: the e-mail is information, never an identity key.
    display_name = (
        _text(claims.get("name"), 200)
        or _text(claims.get("preferred_username"), 200)
        or (email[:200] if email else None)
    )
    department = _text(claims.get(department_claim), 200) if department_claim else None
    return IdentityClaims(
        issuer=idp.issuer,
        subject=subject,
        email=email,
        display_name=display_name,
        department=department,
        raw=dict(claims),
    )


__all__ = ["ALLOWED_ALGORITHMS", "IdentityClaims", "TokenInvalid", "validate_id_token"]
