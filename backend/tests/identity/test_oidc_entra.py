"""The Microsoft Entra ID preset: ID-token rules and the Graph department lookup (mocked shapes)."""

from typing import Any

import pytest

from pharos.identity.oidc.client import GRAPH_ME_URL, IdpClient
from pharos.identity.oidc.tokens import TokenInvalid, validate_id_token

from ..clock import FrozenClock
from ..oidc_fake import (
    CLIENT_ID,
    CLIENT_SECRET,
    ENTRA_ISSUER,
    ENTRA_OBJECT_ID,
    ENTRA_TENANT,
    FakeIdp,
    entra_claims,
    entra_idp,
)

NONCE = "nonce"


class Rig:
    def __init__(self) -> None:
        self.clock = FrozenClock()
        self.idp: FakeIdp = entra_idp(now=lambda: self.clock.now().timestamp())
        self.client = IdpClient(
            self.idp.client(),
            self.clock,
            issuer=ENTRA_ISSUER,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            allow_http=False,
        )

    def token(self, **overrides: Any) -> str:
        return self.idp.sign(
            self.idp.claims(**{"nonce": NONCE, **entra_claims(self.idp, **overrides)})
        )

    async def check(
        self, token: str, tenant: str | None = ENTRA_TENANT, allow_guests: bool = False
    ):
        return await validate_id_token(
            self.client,
            token,
            nonce=NONCE,
            clock=self.clock,
            department_claim=None,
            entra_tenant=tenant,
            entra_allow_guests=allow_guests,
        )

    async def reason(self, token: str) -> str:
        with pytest.raises(TokenInvalid) as caught:
            await self.check(token)
        return caught.value.reason


@pytest.fixture
def rig() -> Rig:
    return Rig()


# --- the ID token --------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_token_of_the_directory_identifies_the_person_by_object_id(rig: Rig) -> None:
    claims = await rig.check(rig.token())
    assert claims.issuer == ENTRA_ISSUER
    assert claims.subject == ENTRA_OBJECT_ID  # not `sub`, which differs per application
    assert claims.display_name == "Test User"
    assert claims.email is None  # Entra often sends none; it would be information only anyway


@pytest.mark.anyio
async def test_the_same_person_in_another_application_is_the_same_subject(rig: Rig) -> None:
    first = await rig.check(rig.token(sub="per-app-subject-one"))
    second = await rig.check(rig.token(sub="per-app-subject-two"))
    assert first.subject == second.subject == ENTRA_OBJECT_ID


@pytest.mark.anyio
async def test_the_object_id_and_tenant_are_compared_without_regard_to_case(rig: Rig) -> None:
    claims = await rig.check(rig.token(oid=ENTRA_OBJECT_ID.upper(), tid=ENTRA_TENANT.upper()))
    assert claims.subject == ENTRA_OBJECT_ID  # stored in one form only


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tid",
    ["9f8e7d6c-5b4a-4c3d-8e2f-1a0b9c8d7e6f", "common", "", None, 7, ["x"]],
)
async def test_a_token_that_names_another_directory_is_refused(rig: Rig, tid: object) -> None:
    # The issuer is ours and the signature is valid: only `tid` is wrong.
    assert await rig.reason(rig.token(tid=tid)) == "bad_tenant"


@pytest.mark.anyio
async def test_a_token_without_tid_is_refused(rig: Rig) -> None:
    assert await rig.reason(rig.token(tid=None)) == "bad_tenant"


@pytest.mark.anyio
@pytest.mark.parametrize("ver", ["1.0", "3.0", 2, None])
async def test_only_version_2_tokens_are_accepted(rig: Rig, ver: object) -> None:
    assert await rig.reason(rig.token(ver=ver)) == "bad_version"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "oid",
    [None, "", "not-a-guid", "11111111-2222-4333-8444-55555555555", 5, ["a"], "x" * 400],
)
async def test_a_missing_or_malformed_object_id_is_refused(rig: Rig, oid: object) -> None:
    assert await rig.reason(rig.token(oid=oid)) == "bad_subject"


@pytest.mark.anyio
async def test_an_issuer_of_another_directory_is_refused_even_when_tid_matches(rig: Rig) -> None:
    other = "https://login.microsoftonline.com/9f8e7d6c-5b4a-4c3d-8e2f-1a0b9c8d7e6f/v2.0"
    token = rig.idp.sign(rig.idp.claims(nonce=NONCE, **entra_claims(rig.idp, iss=other)))
    assert await rig.reason(token) == "bad_claims"


@pytest.mark.anyio
async def test_the_generic_rules_still_apply_to_entra_tokens(rig: Rig) -> None:
    assert await rig.reason(rig.token(aud="someone-else")) == "bad_claims"
    assert await rig.reason(rig.token(nonce="not-ours")) == "bad_nonce"
    assert await rig.reason(rig.token(exp=1)) == "expired"


@pytest.mark.anyio
async def test_without_the_preset_the_entra_claims_play_no_role(rig: Rig) -> None:
    claims = await rig.check(rig.token(tid="anything", ver="1.0"), tenant=None)
    assert claims.subject == entra_claims(rig.idp)["sub"]  # the generic rule: `sub`


# --- Microsoft Graph -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_department_comes_from_graph_with_the_access_token(rig: Rig) -> None:
    assert await rig.client.graph_department("opaque-access-token") == "Finance"
    [(url, authorization)] = rig.idp.graph_requests
    assert url == GRAPH_ME_URL
    assert url.startswith("https://graph.microsoft.com/v1.0/me")
    assert authorization == "Bearer opaque-access-token"


@pytest.mark.anyio
async def test_the_graph_address_is_fixed_not_taken_from_anywhere(rig: Rig) -> None:
    # Whatever the provider's documents or tokens say, the only host the lookup uses is Graph.
    rig.idp.discovery["userinfo_endpoint"] = "https://evil.test/userinfo"
    await rig.client.graph_department("opaque-access-token")
    assert all(url.startswith("https://graph.microsoft.com/") for url, _ in rig.idp.graph_requests)


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
async def test_a_graph_failure_means_no_department_not_an_error(rig: Rig, status: int) -> None:
    rig.idp.graph_status = status
    assert await rig.client.graph_department("opaque-access-token") is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body", [{}, {"department": None}, {"department": 5}, {"department": ["a"]}, ["x"], "text"]
)
async def test_an_unusable_graph_answer_means_no_department(rig: Rig, body: object) -> None:
    rig.idp.graph_body = body
    assert await rig.client.graph_department("opaque-access-token") is None


@pytest.mark.anyio
async def test_a_rejected_access_token_means_no_department(rig: Rig) -> None:
    assert await rig.client.graph_department("some-other-token") is None


# --- guests ----------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "extra",
    [{"acct": 1}, {"idp": "https://sts.windows.net/7c6b5a49-3827-4615-9a03-b1c2d3e4f506/"}],
)
async def test_guests_of_the_directory_are_refused_by_default(
    rig: Rig, extra: dict[str, Any]
) -> None:
    token = rig.token(**extra)
    assert await rig.reason(token) == "guest_not_allowed"
    assert (await rig.check(token, allow_guests=True)).subject == ENTRA_OBJECT_ID


@pytest.mark.anyio
async def test_members_are_not_mistaken_for_guests(rig: Rig) -> None:
    for extra in ({"acct": 0}, {"idp": ENTRA_ISSUER}, {}):
        assert (await rig.check(rig.token(**extra))).subject == ENTRA_OBJECT_ID


def test_the_tokens_do_not_show_in_a_repr() -> None:
    from pharos.identity.oidc.client import TokenSet

    shown = repr(TokenSet("eyJ.secret.id-token", "secret-access-token"))
    assert "secret" not in shown
