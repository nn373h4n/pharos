"""The identity-provider client: what it calls, how strictly, and what it refuses."""

import logging
from pathlib import Path

import httpx
import pytest

from pharos.identity.oidc.client import (
    IdpClient,
    IdpRejected,
    IdpUnavailable,
    parse_discovery,
)

from ..clock import FrozenClock
from ..oidc_fake import CLIENT_ID, CLIENT_SECRET, ISSUER, REDIRECT_URI, FakeIdp, reply


def make(
    idp: FakeIdp,
    clock: FrozenClock | None = None,
    *,
    secret: str | None = CLIENT_SECRET,
    allow_http: bool = False,
) -> IdpClient:
    return IdpClient(
        idp.client(),
        clock or FrozenClock(),
        issuer=idp.issuer,
        client_id=CLIENT_ID,
        client_secret=secret,
        allow_http=allow_http,
    )


# --- discovery ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_discovery_is_read_checked_and_cached() -> None:
    idp = FakeIdp()
    client = make(idp)
    first = await client.discovery()
    assert first.issuer == ISSUER
    assert first.authorization_endpoint.startswith(ISSUER)
    await client.discovery()
    assert len(idp.requests) == 1


@pytest.mark.anyio
async def test_discovery_is_refreshed_after_an_hour() -> None:
    idp, clock = FakeIdp(), FrozenClock()
    client = make(idp, clock)
    await client.discovery()
    clock.advance(minutes=59)
    await client.discovery()
    assert len(idp.requests) == 1
    clock.advance(minutes=2)
    await client.discovery()
    assert len(idp.requests) == 2


@pytest.mark.anyio
async def test_a_provider_that_names_another_issuer_is_refused() -> None:
    idp = FakeIdp()
    idp.discovery = {"issuer": "https://evil.test/realms/pharos"}
    with pytest.raises(IdpUnavailable, match="issuer"):
        await make(idp).discovery()


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
async def test_a_missing_required_endpoint_is_refused(field: str) -> None:
    idp = FakeIdp()
    idp.discovery = {field: None}
    with pytest.raises(IdpUnavailable, match=field):
        await make(idp).discovery()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad",
    [
        "http://idp.test/auth",
        "ftp://idp.test/auth",
        "javascript:alert(1)",
        "https://user:pw@idp.test/auth",
        "https://idp.test/auth#frag",
        "//idp.test/auth",
        "",
        5,
    ],
)
async def test_unacceptable_endpoint_urls_are_refused_in_production_mode(bad: object) -> None:
    idp = FakeIdp()
    idp.discovery = {"token_endpoint": bad}
    with pytest.raises(IdpUnavailable):
        await make(idp).discovery()


@pytest.mark.anyio
async def test_plain_http_endpoints_are_only_for_development() -> None:
    idp = FakeIdp()
    idp.discovery = {"token_endpoint": "http://idp.test/token"}
    with pytest.raises(IdpUnavailable):
        await make(idp, allow_http=False).discovery()
    assert (await make(idp, allow_http=True).discovery()).token_endpoint == "http://idp.test/token"


@pytest.mark.anyio
async def test_a_provider_without_pkce_s256_is_refused() -> None:
    idp = FakeIdp()
    idp.discovery = {"code_challenge_methods_supported": ["plain"]}
    with pytest.raises(IdpUnavailable, match="PKCE"):
        await make(idp).discovery()


@pytest.mark.anyio
async def test_a_provider_that_does_not_list_pkce_methods_is_tolerated() -> None:
    idp = FakeIdp()
    idp.discovery = {"code_challenge_methods_supported": None}
    assert (await make(idp).discovery()).token_endpoint


def test_parse_discovery_needs_the_exact_issuer() -> None:
    document = FakeIdp().discovery_document()
    assert parse_discovery(document, ISSUER, allow_http=False).issuer == ISSUER
    with pytest.raises(IdpUnavailable):
        parse_discovery(document, ISSUER + "/", allow_http=False)


# --- how requests are made ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_redirects_are_not_followed() -> None:
    idp = FakeIdp()
    idp.redirect_to = "https://evil.test/steal"
    with pytest.raises(IdpUnavailable, match="redirect"):
        await make(idp).discovery()
    assert all("evil.test" not in url for _, url in idp.requests)


@pytest.mark.anyio
async def test_oversized_answers_are_refused() -> None:
    idp = FakeIdp()
    idp.oversize = True
    with pytest.raises(IdpUnavailable, match="too large"):
        await make(idp).discovery()


@pytest.mark.anyio
@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_errors_mean_unavailable(status: int) -> None:
    idp = FakeIdp()
    idp.discovery_status = status
    with pytest.raises(IdpUnavailable):
        await make(idp).discovery()


@pytest.mark.anyio
async def test_discovery_answering_not_found_means_unavailable() -> None:
    idp = FakeIdp()
    idp.discovery_status = 404
    with pytest.raises(IdpUnavailable):
        await make(idp).discovery()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [httpx.ConnectTimeout("t"), httpx.ConnectError("c"), httpx.ReadTimeout("r")]
)
async def test_network_problems_mean_unavailable_and_are_not_leaked(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    idp = FakeIdp()
    idp.raise_on = {"/.well-known/openid-configuration": error}
    with caplog.at_level(logging.DEBUG), pytest.raises(IdpUnavailable) as caught:
        await make(idp).discovery()
    # The caller learns only that the provider could not be reached; the log names the error class
    # (as a field) and nothing from the exception text, which can contain URLs with parameters.
    assert str(caught.value) == "the provider could not be reached"
    assert [r.error for r in caplog.records if hasattr(r, "error")] == [type(error).__name__]


@pytest.mark.anyio
async def test_an_answer_that_is_not_a_json_object_is_refused() -> None:
    idp = FakeIdp()
    idp.token_body = b"[1, 2]"
    client = make(idp)
    with pytest.raises(IdpUnavailable, match="JSON object"):
        await client.exchange_code("code", "verifier", REDIRECT_URI)
    idp.token_body = b"<html>"
    with pytest.raises(IdpUnavailable, match="not JSON"):
        await client.exchange_code("code", "verifier", REDIRECT_URI)


# --- the code exchange -------------------------------------------------------------------------


def a_code(idp: FakeIdp, verifier: str = "v" * 64, **claims: object) -> tuple[str, str]:
    import base64
    import hashlib
    from urllib.parse import urlencode

    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    url = (
        idp.discovery_document()["authorization_endpoint"]
        + "?"
        + urlencode(
            {
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "code_challenge_method": "S256",
                "code_challenge": challenge.decode(),
                "state": "s",
                "nonce": "n",
            }
        )
    )
    code, _ = idp.authorize(url, **claims)
    return code, verifier


@pytest.mark.anyio
async def test_the_code_is_exchanged_with_client_authentication_and_pkce() -> None:
    idp = FakeIdp()
    code, verifier = a_code(idp)
    token = await make(idp).exchange_code(code, verifier, REDIRECT_URI)
    assert token.count(".") == 2
    method, url = idp.requests[-1]
    assert (method, url) == ("POST", f"{ISSUER}/protocol/openid-connect/token")


@pytest.mark.anyio
async def test_a_wrong_pkce_verifier_is_rejected_by_the_provider() -> None:
    idp = FakeIdp()
    code, _ = a_code(idp)
    with pytest.raises(IdpRejected):
        await make(idp).exchange_code(code, "w" * 64, REDIRECT_URI)


@pytest.mark.anyio
async def test_a_code_can_be_used_once() -> None:
    idp = FakeIdp()
    code, verifier = a_code(idp)
    client = make(idp)
    await client.exchange_code(code, verifier, REDIRECT_URI)
    with pytest.raises(IdpRejected):
        await client.exchange_code(code, verifier, REDIRECT_URI)


@pytest.mark.anyio
async def test_the_provider_refusing_the_code_is_a_rejection_not_an_outage() -> None:
    idp = FakeIdp()
    idp.token_status = 400
    with pytest.raises(IdpRejected):
        await make(idp).exchange_code("c", "v", REDIRECT_URI)
    idp.token_status = 503
    with pytest.raises(IdpUnavailable):
        await make(idp).exchange_code("c", "v", REDIRECT_URI)


@pytest.mark.anyio
async def test_a_token_response_without_an_id_token_is_refused() -> None:
    idp = FakeIdp()
    idp.token_body = b'{"access_token": "x"}'
    with pytest.raises(IdpUnavailable, match="no ID token"):
        await make(idp).exchange_code("c", "v", REDIRECT_URI)


@pytest.mark.anyio
async def test_the_client_secret_goes_in_the_authorization_header_not_the_url_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    idp = FakeIdp()
    code, verifier = a_code(idp)
    with caplog.at_level(logging.DEBUG):
        await make(idp).exchange_code(code, verifier, REDIRECT_URI)
    assert all(CLIENT_SECRET not in url for _, url in idp.requests)
    assert CLIENT_SECRET not in caplog.text


@pytest.mark.anyio
async def test_a_wrong_client_secret_is_rejected_by_the_provider() -> None:
    idp = FakeIdp()
    code, verifier = a_code(idp)
    with pytest.raises(IdpRejected):
        await make(idp, secret="not-the-secret").exchange_code(code, verifier, REDIRECT_URI)


@pytest.mark.anyio
async def test_a_public_client_sends_its_id_instead_of_a_secret() -> None:
    idp = FakeIdp()
    idp.require_basic_auth = False
    sent: list[str] = []
    original = idp.handler

    def spy(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            sent.append(request.content.decode())
        return original(request)

    client = IdpClient(
        httpx.AsyncClient(transport=httpx.MockTransport(spy)),
        FrozenClock(),
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=None,
        allow_http=False,
    )
    code, verifier = a_code(idp)
    await client.exchange_code(code, verifier, REDIRECT_URI)
    assert f"client_id={CLIENT_ID}" in sent[0]
    assert "client_secret" not in sent[0]


@pytest.mark.anyio
async def test_only_endpoints_from_the_discovery_document_are_called() -> None:
    idp = FakeIdp()
    code, verifier = a_code(idp)
    client = make(idp)
    await client.exchange_code(code, verifier, REDIRECT_URI)
    await client.key_set()
    allowed = {
        f"{ISSUER}/.well-known/openid-configuration",
        f"{ISSUER}/protocol/openid-connect/token",
        f"{ISSUER}/protocol/openid-connect/certs",
    }
    assert {url for _, url in idp.requests} <= allowed


@pytest.mark.anyio
async def test_a_key_set_that_is_not_usable_is_refused() -> None:
    idp = FakeIdp()
    original = idp.handler

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/certs"):
            return reply(200, json_={"keys": "not a list"})
        return original(request)

    client = IdpClient(
        httpx.AsyncClient(transport=httpx.MockTransport(broken)),
        FrozenClock(),
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        allow_http=False,
    )
    with pytest.raises(IdpUnavailable):
        await client.key_set()


@pytest.mark.anyio
async def test_the_client_is_made_on_first_use_so_a_missing_certificate_file_does_not_stop_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "not-there-yet.crt"))
    client = IdpClient(
        None,
        FrozenClock(),
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=None,
        allow_http=False,
    )  # does not raise
    with pytest.raises(IdpUnavailable, match="certificates"):
        await client.discovery()
    await client.aclose()  # and closing a client that never opened is fine


@pytest.mark.anyio
async def test_proxy_variables_do_not_reach_the_provider_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:3128")
    client = IdpClient(
        None,
        FrozenClock(),
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        allow_http=False,
    )
    assert client._http._trust_env is False
    assert not client._http._mounts
    await client.aclose()


# --- review findings ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_secret_with_special_characters_is_form_encoded_in_the_basic_header() -> None:
    idp = FakeIdp()
    idp.client_secret = "a:b+c%d e"
    code, verifier = a_code(idp)
    client = make(idp, secret="a:b+c%d e")
    assert (await client.exchange_code(code, verifier, REDIRECT_URI)).count(".") == 2


@pytest.mark.anyio
async def test_a_small_compressed_answer_cannot_inflate_past_the_size_limit() -> None:
    idp = FakeIdp()
    idp.gzip_bomb = True
    # Compressed bytes are read as they are (about 50 KB) and are not JSON; they are never
    # inflated to the 50 MB they would become.
    with pytest.raises(IdpUnavailable, match="not JSON"):
        await make(idp).discovery()


@pytest.mark.anyio
async def test_requests_ask_for_uncompressed_answers() -> None:
    idp = FakeIdp()
    seen: list[str] = []
    original = idp.handler

    def spy(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding", ""))
        return original(request)

    client = IdpClient(
        httpx.AsyncClient(transport=httpx.MockTransport(spy)),
        FrozenClock(),
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=None,
        allow_http=False,
    )
    await client.discovery()
    assert seen == ["identity"]


@pytest.mark.anyio
@pytest.mark.parametrize("error", [httpx.InvalidURL("bad"), httpx.StreamConsumed()])
async def test_url_and_stream_errors_from_httpx_are_unavailable_not_a_crash(
    error: Exception,
) -> None:
    idp = FakeIdp()
    client = make(idp)

    def broken(*_: object, **__: object) -> object:
        raise error

    client._given_http.stream = broken  # type: ignore[union-attr,method-assign]
    with pytest.raises(IdpUnavailable):
        await client.discovery()


@pytest.mark.anyio
async def test_concurrent_callers_share_one_discovery_and_one_key_fetch() -> None:
    import asyncio

    idp = FakeIdp()

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)  # callers overlap, as they do on a busy server
        return idp.handler(request)

    client = IdpClient(
        httpx.AsyncClient(transport=httpx.MockTransport(slow)),
        FrozenClock(),
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        allow_http=False,
    )
    await asyncio.gather(*(client.discovery() for _ in range(20)))
    await asyncio.gather(*(client.key_set() for _ in range(20)))
    assert len([r for r in idp.requests if r[1].endswith("openid-configuration")]) == 1
    assert len([r for r in idp.requests if r[1].endswith("/certs")]) == 1


@pytest.mark.anyio
async def test_the_iss_parameter_requirement_follows_the_provider() -> None:
    idp = FakeIdp()
    assert (await make(idp).discovery()).iss_parameter_required is False
    idp.discovery = {"authorization_response_iss_parameter_supported": True}
    assert (await make(idp).discovery()).iss_parameter_required is True
