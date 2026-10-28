"""ID-token validation against a fake provider. Each test is one way a token can be wrong."""

import pytest

from pharos.identity.oidc.client import IdpClient
from pharos.identity.oidc.tokens import TokenInvalid, validate_id_token

from ..clock import FrozenClock
from ..oidc_fake import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIdp, b64, compact

NONCE = "the-nonce-we-sent"


class Rig:
    def __init__(self) -> None:
        self.clock = FrozenClock()
        self.idp = FakeIdp(now=lambda: self.clock.now().timestamp())
        self.client = IdpClient(
            self.idp.client(),
            self.clock,
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            allow_http=False,
        )

    def claims(self, **overrides: object) -> dict[str, object]:
        return self.idp.claims(nonce=NONCE, **overrides)

    async def check(self, token: str, department_claim: str | None = "department"):
        return await validate_id_token(
            self.client, token, nonce=NONCE, clock=self.clock, department_claim=department_claim
        )

    async def reason(self, token: str) -> str:
        with pytest.raises(TokenInvalid) as caught:
            await self.check(token)
        return caught.value.reason


@pytest.fixture
def rig() -> Rig:
    return Rig()


# --- accepted ----------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("alg", ["RS256", "ES256"])
async def test_a_valid_token_is_accepted(rig: Rig, alg: str) -> None:
    claims = await rig.check(rig.idp.sign(rig.claims(department="Finance"), alg=alg))
    assert claims.subject == "subject-1"
    assert claims.issuer == ISSUER
    assert claims.email == "user@example.test"
    assert claims.display_name == "Test User"
    assert claims.department == "Finance"


@pytest.mark.anyio
async def test_an_audience_list_that_names_us_with_a_matching_azp_is_accepted(rig: Rig) -> None:
    token = rig.idp.sign(rig.claims(aud=[CLIENT_ID, "other-api"], azp=CLIENT_ID))
    assert (await rig.check(token)).subject == "subject-1"


@pytest.mark.anyio
async def test_the_leeway_is_a_minute_not_more(rig: Rig) -> None:
    now = int(rig.clock.now().timestamp())
    just_expired = rig.idp.sign(rig.claims(exp=now - 30))
    assert (await rig.check(just_expired)).subject  # within the 60 s leeway
    assert await rig.reason(rig.idp.sign(rig.claims(exp=now - 120))) == "expired"
    assert (await rig.check(rig.idp.sign(rig.claims(iat=now + 30)))).subject
    assert await rig.reason(rig.idp.sign(rig.claims(iat=now + 300))) == "bad_claims"


# --- wrong claims ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_another_issuer_is_refused(rig: Rig) -> None:
    assert await rig.reason(rig.idp.sign(rig.claims(iss="https://evil.test/realms/pharos"))) == (
        "bad_claims"
    )
    assert await rig.reason(rig.idp.sign(rig.claims(iss=ISSUER + "/"))) == "bad_claims"


@pytest.mark.anyio
@pytest.mark.parametrize("audience", ["someone-else", ["someone-else"], [], ""])
async def test_a_token_for_another_audience_is_refused(rig: Rig, audience: object) -> None:
    assert await rig.reason(rig.idp.sign(rig.claims(aud=audience))) == "bad_claims"


@pytest.mark.anyio
async def test_several_audiences_need_our_client_as_the_authorized_party(rig: Rig) -> None:
    both = [CLIENT_ID, "other-api"]
    assert await rig.reason(rig.idp.sign(rig.claims(aud=both))) == "bad_authorized_party"
    assert (
        await rig.reason(rig.idp.sign(rig.claims(aud=both, azp="other-api")))
        == "bad_authorized_party"
    )


@pytest.mark.anyio
async def test_an_authorized_party_that_is_not_us_is_refused_even_for_one_audience(
    rig: Rig,
) -> None:
    assert await rig.reason(rig.idp.sign(rig.claims(azp="other-client"))) == "bad_authorized_party"


@pytest.mark.anyio
@pytest.mark.parametrize("claim", ["exp", "iat", "sub", "iss", "aud"])
async def test_a_missing_required_claim_is_refused(rig: Rig, claim: str) -> None:
    claims = rig.claims()
    del claims[claim]
    assert await rig.reason(rig.idp.sign(claims)) == "bad_claims"


@pytest.mark.anyio
@pytest.mark.parametrize("nonce", ["another-nonce", "", None, 12345])
async def test_a_nonce_we_did_not_send_is_refused(rig: Rig, nonce: object) -> None:
    claims = rig.claims()
    claims["nonce"] = nonce
    if nonce is None:
        del claims["nonce"]
    assert await rig.reason(rig.idp.sign(claims)) == "bad_nonce"


@pytest.mark.anyio
@pytest.mark.parametrize("subject", ["", "x" * 257, "bad\nsubject", "nul\x00", 42, None])
async def test_an_unusable_subject_is_refused(rig: Rig, subject: object) -> None:
    claims = rig.claims()
    claims["sub"] = subject
    if subject is None:
        del claims["sub"]
    assert await rig.reason(rig.idp.sign(claims)) in {"bad_subject", "bad_claims"}


@pytest.mark.anyio
async def test_text_claims_are_cleaned_and_bounded(rig: Rig) -> None:
    token = rig.idp.sign(
        rig.claims(
            name="  Ann\x00 \x1bLee  ",
            email="a" * 400 + "@example.test",
            department="Fin\nance",
        )
    )
    claims = await rig.check(token)
    assert claims.display_name == "Ann Lee"
    assert claims.email is None or len(claims.email) <= 320
    assert claims.department == "Finance"


@pytest.mark.anyio
async def test_the_name_falls_back_to_the_username_then_the_email(rig: Rig) -> None:
    token = rig.idp.sign(rig.claims(name=None, preferred_username="ann.lee"))
    assert (await rig.check(token)).display_name == "ann.lee"
    token = rig.idp.sign(rig.claims(name=None))
    assert (await rig.check(token)).display_name == "user@example.test"


@pytest.mark.anyio
async def test_no_department_claim_configured_or_sent_gives_none(rig: Rig) -> None:
    token = rig.idp.sign(rig.claims(department="Finance"))
    assert (await rig.check(token, department_claim=None)).department is None
    assert (await rig.check(rig.idp.sign(rig.claims()))).department is None


# --- algorithms and signatures -----------------------------------------------------------------


@pytest.mark.anyio
async def test_alg_none_is_refused(rig: Rig) -> None:
    assert await rig.reason(rig.idp.none_token(rig.claims())) == "unsupported_algorithm"


@pytest.mark.anyio
async def test_an_hmac_token_keyed_with_the_public_key_is_refused(rig: Rig) -> None:
    assert await rig.reason(rig.idp.hs256_token(rig.claims())) == "unsupported_algorithm"


@pytest.mark.anyio
async def test_an_algorithm_outside_our_list_is_refused_even_with_a_valid_key(rig: Rig) -> None:
    # RS512 is a fine algorithm, but the list is fixed in our code (the signature is irrelevant).
    token = compact({"alg": "RS512", "kid": "rsa-1"}, rig.claims(), b"signature")
    assert await rig.reason(token) == "unsupported_algorithm"


@pytest.mark.anyio
async def test_a_token_signed_by_another_key_is_refused(rig: Rig) -> None:
    other = FakeIdp()
    forged = other.sign(rig.claims())  # same kid "rsa-1", different key material
    assert await rig.reason(forged) == "bad_signature"


@pytest.mark.anyio
async def test_a_tampered_payload_is_refused(rig: Rig) -> None:
    head, _, signature = rig.idp.sign(rig.claims()).split(".")
    import json

    evil = b64(json.dumps(rig.claims(sub="admin")).encode())
    assert await rig.reason(f"{head}.{evil}.{signature}") == "bad_signature"


@pytest.mark.anyio
@pytest.mark.parametrize("token", ["", "abc", "a.b", "a.b.c", "....", "x" * 50, "e30.e30."])
async def test_garbage_is_refused(rig: Rig, token: str) -> None:
    assert await rig.reason(token) in {"malformed", "unsupported_algorithm"}


@pytest.mark.anyio
async def test_an_oversized_token_is_refused_before_any_work(rig: Rig) -> None:
    assert await rig.reason("a" * 20_000) == "too_large"
    assert rig.idp.requests == []  # nothing was fetched for it


@pytest.mark.anyio
async def test_a_header_without_a_usable_alg_is_refused(rig: Rig) -> None:
    for header in ({}, {"alg": 5}, {"alg": ["RS256"]}):
        reason = await rig.reason(compact(header, rig.claims(), b"sig"))
        assert reason in {"malformed", "unsupported_algorithm"}


# --- keys --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_rotated_key_is_picked_up_with_one_refetch(rig: Rig) -> None:
    await rig.check(rig.idp.sign(rig.claims()))  # caches the key set (rsa-1)
    rig.idp.rotate()  # the provider now signs with rsa-2
    rig.clock.advance(seconds=120)  # long enough since the last fetch
    token = rig.idp.sign(rig.claims())
    assert (await rig.check(token)).subject == "subject-1"
    jwks_fetches = [r for r in rig.idp.requests if r[1].endswith("/certs")]
    assert len(jwks_fetches) == 2


@pytest.mark.anyio
async def test_an_unknown_key_after_one_refetch_is_refused(rig: Rig) -> None:
    await rig.check(rig.idp.sign(rig.claims()))
    rig.clock.advance(seconds=120)
    stranger = FakeIdp()
    stranger.rsa = stranger.rsa  # a key the provider never published
    forged = stranger.sign(rig.claims(), key=_with_kid(stranger.rsa, "never-published"))
    assert await rig.reason(forged) == "unknown_key"
    jwks_fetches = [r for r in rig.idp.requests if r[1].endswith("/certs")]
    assert len(jwks_fetches) == 2  # one more look, not a loop


@pytest.mark.anyio
async def test_refetching_keys_is_rate_limited(rig: Rig) -> None:
    await rig.check(rig.idp.sign(rig.claims()))
    stranger = FakeIdp()
    forged = stranger.sign(rig.claims(), key=_with_kid(stranger.rsa, "never-published"))
    for _ in range(20):  # a stream of bogus tokens
        assert await rig.reason(forged) == "unknown_key"
    jwks_fetches = [r for r in rig.idp.requests if r[1].endswith("/certs")]
    assert len(jwks_fetches) == 1  # inside the first minute no refetch happens at all
    rig.clock.advance(seconds=61)
    assert await rig.reason(forged) == "unknown_key"
    assert len([r for r in rig.idp.requests if r[1].endswith("/certs")]) == 2


def _with_kid(key: object, kid: str) -> object:
    from joserfc.jwk import RSAKey

    assert isinstance(key, RSAKey)
    return RSAKey.import_key(key.as_dict(private=True), parameters={"kid": kid, "use": "sig"})


@pytest.mark.anyio
async def test_a_non_ascii_nonce_is_a_refusal_not_a_crash(rig: Rig) -> None:
    claims = rig.claims()
    claims["nonce"] = "n\u00f6nce-\u0259"
    assert await rig.reason(rig.idp.sign(claims)) == "bad_nonce"


@pytest.mark.anyio
async def test_a_very_long_e_mail_cannot_become_a_name_that_does_not_fit(rig: Rig) -> None:
    long_mail = "a" * 300 + "@example.test"
    claims = await rig.check(rig.idp.sign(rig.claims(name=None, email=long_mail)))
    assert claims.display_name is not None
    assert len(claims.display_name) <= 200  # users_display_name_check
