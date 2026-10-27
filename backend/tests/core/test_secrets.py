import pytest
from pydantic import SecretStr

from pharos.core.secrets import KeyPurpose, derive_key, hkdf

from ..constants import FAKE_SECRET_KEY


def test_hkdf_matches_the_rfc_5869_test_vector() -> None:
    # RFC 5869, appendix A.1 (SHA-256).
    okm = hkdf(
        bytes.fromhex("0b" * 22),
        bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"),
        length=42,
        salt=bytes.fromhex("000102030405060708090a0b0c"),
    )
    assert okm.hex() == (
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    )


def test_each_purpose_gets_its_own_key() -> None:
    secret = SecretStr(FAKE_SECRET_KEY)
    keys = {derive_key(secret, purpose) for purpose in KeyPurpose}
    assert len(keys) == len(KeyPurpose)
    assert all(len(key) == 32 for key in keys)


def test_derivation_is_stable_and_depends_on_the_root_secret() -> None:
    a, b = SecretStr("a" * 16 + "b" * 16), SecretStr("b" * 16 + "a" * 16)
    assert derive_key(a, KeyPurpose.CSRF) == derive_key(a, KeyPurpose.CSRF)
    assert derive_key(a, KeyPurpose.CSRF) != derive_key(b, KeyPurpose.CSRF)


def test_hkdf_refuses_an_oversized_request() -> None:
    with pytest.raises(ValueError, match="too long"):
        hkdf(b"k", b"i", length=255 * 32 + 1)
