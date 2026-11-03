"""The local model (M4-10, ADR-0020): one fixed address inside the Compose network, no key, no
cost per token, no redirect followed. The real LiteLLM and the real HTTP client talk to a small
server on the loopback that answers like Ollama's OpenAI-compatible API."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from pydantic import SecretStr

from pharos.core.config import Settings
from pharos.llm import litellm_provider
from pharos.llm.errors import ProviderFailed, ProviderRejected
from pharos.llm.factory import build_provider, gateway_config
from pharos.llm.litellm_provider import LiteLLMProvider, local_model_url

pytestmark = pytest.mark.anyio


class Local:
    """A server that records what it was sent and answers as scripted."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.headers: list[str] = []
        self.redirect_hits = 0
        self.mode = "ok"
        self.vectors: list[list[float]] = [[0.25] * 16]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return None

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                outer.paths.append(self.path)
                outer.headers.append(self.headers.get("authorization", ""))
                if outer.mode == "redirect":
                    self.send_response(307)
                    self.send_header("location", f"http://127.0.0.1:{outer.port}/elsewhere")
                    self.end_headers()
                    return
                if self.path.startswith("/elsewhere"):
                    outer.redirect_hits += 1
                if self.path.endswith("/embeddings"):
                    vectors = outer.vectors
                    payload = json.dumps(
                        {
                            "object": "list",
                            "model": body.get("model", ""),
                            "data": [
                                {"object": "embedding", "index": i, "embedding": v}
                                for i, v in enumerate(vectors)
                            ],
                            "usage": {"prompt_tokens": 7, "total_tokens": 7},
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                answer = {
                    "id": "x",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body.get("model", ""),
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": '{"ok": true}'},
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
                }
                payload = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


@pytest.fixture
def local() -> Iterator[Local]:
    found = Local()
    yield found
    found.server.shutdown()
    found.server.server_close()


def provider(url: str | None) -> LiteLLMProvider:
    return LiteLLMProvider("ollama", SecretStr("ollama"), base_url=url)


async def ask(found: LiteLLMProvider) -> Any:
    return await found.complete(
        model="qwen2.5:7b", system="rules", user="task", max_tokens=50, deadline_seconds=10.0
    )


class TestTheRequest:
    async def test_a_round_trip_with_the_real_library_sends_text_only_to_the_chat_endpoint(
        self, local: Local
    ) -> None:
        answer = await ask(provider(local.url))
        assert (answer.text, answer.tokens_in, answer.tokens_out) == ('{"ok": true}', 12, 5)
        [body] = local.requests
        assert local.paths == ["/v1/chat/completions"]
        assert body["model"] == "qwen2.5:7b"  # the model name is passed as the operator wrote it
        assert body["messages"] == [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "task"},
        ]
        for forbidden in ("tools", "functions", "tool_choice", "function_call"):
            assert forbidden not in body
        assert not body.get("stream")

    async def test_the_placeholder_is_all_that_is_sent_as_a_key(self, local: Local) -> None:
        await ask(provider(local.url))
        assert local.headers == ["Bearer ollama"]

    async def test_a_redirect_is_not_followed(self, local: Local) -> None:
        local.mode = "redirect"
        with pytest.raises((ProviderFailed, ProviderRejected)):
            await ask(provider(local.url))
        assert local.redirect_hits == 0 and len(local.requests) == 1

    async def test_there_is_no_default_address_and_the_built_one_is_an_ip_not_a_name(self) -> None:
        with pytest.raises(ValueError, match="address"):
            LiteLLMProvider("ollama", SecretStr("ollama"))
        assert local_model_url("172.28.30.10") == "http://172.28.30.10:11434/v1"
        built = build_provider(Settings(environment="test", llm_provider="ollama"))  # type: ignore[arg-type]
        assert isinstance(built, LiteLLMProvider)
        assert built._base_url == "http://172.28.30.10:11434/v1"

    async def test_a_proxy_of_the_environment_does_not_carry_the_prompts_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, local: Local
    ) -> None:
        """A search domain or an HTTP_PROXY must not be able to take the local model's traffic."""
        proxy = Local()
        try:
            for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
                monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
            for name in ("NO_PROXY", "no_proxy"):
                monkeypatch.setenv(name, "")
            await ask(provider(local.url))
            assert len(local.requests) == 1 and proxy.requests == []  # direct, not via the proxy

            # Positive control: a remote provider still honours the environment's proxy.
            class Looked:
                async def ensure_public(self, _url: str) -> None:
                    return None

            remote = LiteLLMProvider(
                "openai",
                SecretStr("k"),
                base_url=local.url,
                endpoint=Looked(),  # type: ignore[arg-type]
            )
            await ask(remote)
            assert len(proxy.requests) == 1
        finally:
            proxy.server.shutdown()
            proxy.server.server_close()

    async def test_no_public_name_check_is_made_for_the_local_address_but_still_for_a_remote_one(
        self, monkeypatch: pytest.MonkeyPatch, local: Local
    ) -> None:
        looked: list[str] = []

        class Looked:
            async def ensure_public(self, url: str) -> None:
                looked.append(url)

        monkeypatch.setattr(litellm_provider, "_quiet_litellm", litellm_provider._quiet_litellm)
        await ask(LiteLLMProvider("ollama", SecretStr("o"), base_url=local.url, endpoint=Looked()))  # type: ignore[arg-type]
        assert looked == []  # inside the network on purpose
        remote = LiteLLMProvider(
            "openai",
            SecretStr("k"),
            base_url=local.url,
            endpoint=Looked(),  # type: ignore[arg-type]
        )
        await ask(remote)
        assert looked == [local.url]  # positive control: a remote endpoint is still looked up


class TestTheSettings:
    @pytest.mark.parametrize(
        "extra", [{"llm_base_url": "https://llm.example.org"}, {"llm_api_key": "KEY"}]
    )
    def test_an_address_or_a_key_with_the_local_model_is_refused(
        self, extra: dict[str, str]
    ) -> None:
        with pytest.raises(ValueError, match="ollama"):
            Settings(environment="test", llm_provider="ollama", **extra)  # type: ignore[arg-type]


class TestTheAddressSetting:
    @pytest.mark.parametrize("value", ["10.1.2.3", "172.16.0.9", "192.168.5.5", "172.28.30.10"])
    def test_a_private_address_is_accepted(self, value: str) -> None:
        assert Settings(environment="test", ollama_ip=value).ollama_ip == value  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "value",
        [
            "ollama",  # a name could resolve elsewhere
            "8.8.8.8",
            "127.0.0.1",
            "169.254.169.254",
            "0.0.0.0",  # noqa: S104 (a value the setting must refuse, not an address bound)
            "224.0.0.1",
            "::1",
            "fd00::1",
            "10.0.0.1/24",
            "",
            "10.0.0",
        ],
    )
    def test_anything_else_is_refused(self, value: str) -> None:
        with pytest.raises(ValueError, match="ollama_ip"):
            Settings(environment="test", ollama_ip=value)  # type: ignore[arg-type]


class TestTheGateway:
    def test_the_factory_builds_the_local_provider_without_a_key(self) -> None:
        built = build_provider(Settings(environment="test", llm_provider="ollama"))  # type: ignore[arg-type]
        assert isinstance(built, LiteLLMProvider) and built.name == "ollama"

    def test_a_local_model_costs_nothing_per_token_and_a_remote_one_does(self) -> None:
        local = gateway_config(Settings(environment="test", llm_provider="ollama"))  # type: ignore[arg-type]
        remote = gateway_config(Settings(environment="test", llm_provider="anthropic"))  # type: ignore[arg-type]
        assert (local.price_in, local.price_out) == (0, 0)
        assert remote.price_in > 0 and remote.price_out > 0


class TestEmbeddings:
    async def test_a_round_trip_with_the_real_library_returns_the_vector_and_the_tokens(
        self, local: Local
    ) -> None:
        got = await provider(local.url).embed(
            model="bge-m3", texts=["Acme ships Widget 2"], deadline_seconds=10.0
        )
        assert got.vectors == [[0.25] * 16] and got.tokens_in == 7
        assert local.paths == ["/v1/embeddings"]
        [body] = local.requests
        assert body["model"] == "bge-m3" and body["input"] == ["Acme ships Widget 2"]
        for forbidden in ("tools", "messages", "functions"):
            assert forbidden not in body
        assert local.headers == ["Bearer ollama"]

    async def test_a_redirect_is_not_followed(self, local: Local) -> None:
        local.mode = "redirect"
        with pytest.raises((ProviderFailed, ProviderRejected)):
            await provider(local.url).embed(model="m", texts=["t"], deadline_seconds=10.0)
        assert local.redirect_hits == 0

    async def test_an_answer_without_vectors_is_a_failure_not_a_crash(self, local: Local) -> None:
        local.vectors = []
        got = await provider(local.url).embed(model="m", texts=["t"], deadline_seconds=10.0)
        assert got.vectors == []  # the gateway's check refuses it (SchemaRejected), tested there

    async def test_the_environments_proxy_does_not_take_the_texts_of_the_local_model(
        self, monkeypatch: pytest.MonkeyPatch, local: Local
    ) -> None:
        proxy = Local()
        try:
            for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
                monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
            for name in ("NO_PROXY", "no_proxy"):
                monkeypatch.setenv(name, "")
            await provider(local.url).embed(model="m", texts=["t"], deadline_seconds=10.0)
            assert len(local.requests) == 1 and proxy.requests == []
        finally:
            proxy.server.shutdown()
            proxy.server.server_close()
