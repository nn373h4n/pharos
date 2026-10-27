"""Purpose-bound keys derived from the one root secret (`PHAROS_SECRET_KEY`).

HKDF (RFC 5869, SHA-256) with the purpose as `info`, so the key that signs CSRF tokens can never be
used for anything else, and a leak of one derived key does not reveal the root or its siblings.
"""

import hashlib
import hmac
from enum import StrEnum

from pydantic import SecretStr

_HASH = hashlib.sha256
_HASH_LENGTH = _HASH().digest_size
_SALT = b"pharos/v1"


class KeyPurpose(StrEnum):
    CSRF = "csrf"
    AUDIT_IP = "audit-ip"


def hkdf(key_material: bytes, info: bytes, length: int = 32, salt: bytes = _SALT) -> bytes:
    if length > 255 * _HASH_LENGTH:
        raise ValueError("requested key is too long")
    prk = hmac.new(salt or bytes(_HASH_LENGTH), key_material, _HASH).digest()
    output, block = b"", b""
    for counter in range(1, -(-length // _HASH_LENGTH) + 1):
        block = hmac.new(prk, block + info + bytes([counter]), _HASH).digest()
        output += block
    return output[:length]


def derive_key(secret_key: SecretStr, purpose: KeyPurpose) -> bytes:
    return hkdf(secret_key.get_secret_value().encode(), purpose.value.encode())
