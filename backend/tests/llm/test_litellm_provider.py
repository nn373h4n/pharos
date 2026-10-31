"""The one module that talks to LiteLLM: no tools, no retries of its own, errors sorted into the two
kinds the gateway understands, and LiteLLM kept quiet. LiteLLM is replaced by a stand-in here."""

import asyncio
import os
import socket
import subprocess
import sys
import textwrap
import types
from typing import Any

import litellm
import pytest
from pydantic import SecretStr

from pharos.llm import litellm_provider
from pharos.llm.errors import LLMNotConfigured, ProviderFailed, ProviderRejected
from pharos.llm.litellm_provider import LiteLLMProvider

pytestmark = pytest.mark.anyio

KEY = SecretStr("KEY-SENTINEL-do-not-log")


class StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("provider said no")
        self.status_code = status_code


def response(text: str | None = "{}", tokens: tuple[int, int] = (11, 7)) -> Any:
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))],
        usage=types.SimpleNamespace(prompt_tokens=tokens[0], completion_tokens=tokens[1]),
    )


class Stand:
    """A stand-in for the litellm module. It carries LiteLLM's real exception classes, because the
    provider sorts errors by them (a LiteLLM timeout has status 408, a broken connection 500)."""

    Timeout = litellm.Timeout
    APIConnectionError = litellm.APIConnectionError

    def __init__(self, outcome: Any = None) -> None:
        self.outcome = outcome if outcome is not None else response()
        self.calls: list[dict[str, Any]] = []

    async def acompletion(self, **arguments: Any) -> Any:
        self.calls.append(arguments)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            return await self.outcome()
        return self.outcome


@pytest.fixture
def stand(monkeypatch: pytest.MonkeyPatch) -> Stand:
    fake = Stand()
    monkeypatch.setattr(litellm_provider, "_quiet_litellm", lambda: fake)
    return fake


async def ask(provider: LiteLLMProvider, **changes: Any) -> Any:
    arguments: dict[str, Any] = {
        "model": "claude-test",
        "system": "rules",
        "user": "task",
        "max_tokens": 50,
        "deadline_seconds": 5.0,
    }
    arguments.update(changes)
    return await provider.complete(**arguments)


class TestTheRequest:
    async def test_it_sends_text_and_never_tools(self, stand: Stand) -> None:
        answer = await ask(LiteLLMProvider("anthropic", KEY))
        [call] = stand.calls
        assert (answer.text, answer.tokens_in, answer.tokens_out) == ("{}", 11, 7)
        assert call["model"] == "anthropic/claude-test"
        assert call["messages"] == [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "task"},
        ]
        for forbidden in ("tools", "functions", "tool_choice", "function_call", "stream"):
            assert forbidden not in call
        assert (call["max_tokens"], call["temperature"], call["num_retries"]) == (50, 0, 0)
        assert call["timeout"] == 5.0 and call["api_key"] == "KEY-SENTINEL-do-not-log"

    async def test_an_endpoint_is_sent_only_when_one_was_configured(self, stand: Stand) -> None:
        await ask(LiteLLMProvider("anthropic", KEY))
        await ask(LiteLLMProvider("azure", KEY, base_url="https://example.openai.azure.com"))
        assert "api_base" not in stand.calls[0]
        assert stand.calls[1]["api_base"] == "https://example.openai.azure.com"
        assert stand.calls[1]["model"] == "azure/claude-test"

    @pytest.mark.parametrize("provider", ["anthropic", "openai", "azure"])
    async def test_a_configured_endpoint_gets_a_client_that_follows_no_redirect(
        self, stand: Stand, provider: str
    ) -> None:
        class Looked:
            async def ensure_public(self, _url: str) -> None:
                return None

        endpoint: Any = Looked()
        await ask(
            LiteLLMProvider(provider, KEY, base_url="https://llm.example.org", endpoint=endpoint)
        )
        [call] = stand.calls
        client = call["client"]
        inner = getattr(client, "client", None) or client._client
        assert inner.follow_redirects is False
        await ask(LiteLLMProvider(provider, KEY))
        assert "client" not in stand.calls[1]

    def test_an_unknown_provider_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown provider"):
            LiteLLMProvider("someone", KEY)

    def test_the_key_does_not_show_in_the_provider(self) -> None:
        provider = LiteLLMProvider("anthropic", KEY)
        assert "KEY-SENTINEL" not in repr(provider) and "KEY-SENTINEL" not in str(vars(provider))


class TestErrorsAreSortedIntoTwoKinds:
    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    async def test_a_wrong_key_or_a_refused_request_cannot_be_helped_by_trying_again(
        self, stand: Stand, status: int
    ) -> None:
        stand.outcome = StatusError(status)
        with pytest.raises(ProviderRejected):
            await ask(LiteLLMProvider("anthropic", KEY))

    @pytest.mark.parametrize(
        ("error", "may_have_been_billed"),
        [
            (StatusError(429), False),  # the provider answered with an error status
            (StatusError(500), False),
            (StatusError(529), False),
            (OSError(), True),  # a connection that broke: the request may have got through
        ],
    )
    async def test_a_busy_or_broken_provider_is_worth_another_try_and_says_if_it_billed(
        self, stand: Stand, error: Exception, may_have_been_billed: bool
    ) -> None:
        stand.outcome = error
        with pytest.raises(ProviderFailed) as caught:
            await ask(LiteLLMProvider("anthropic", KEY))
        assert caught.value.sent is may_have_been_billed

    @pytest.mark.parametrize(
        ("error", "may_have_been_billed"),
        [
            (litellm.Timeout(message="t", model="m", llm_provider="anthropic"), True),
            (litellm.APIConnectionError(message="c", llm_provider="anthropic", model="m"), True),
            (litellm.InternalServerError(message="s", llm_provider="anthropic", model="m"), False),
            (litellm.RateLimitError(message="r", llm_provider="anthropic", model="m"), False),
            (
                litellm.ServiceUnavailableError(message="u", llm_provider="anthropic", model="m"),
                False,
            ),
        ],
    )
    async def test_litellms_own_errors_are_sorted_by_whether_the_request_may_have_been_billed(
        self, stand: Stand, error: Exception, may_have_been_billed: bool
    ) -> None:
        stand.outcome = error
        with pytest.raises(ProviderFailed) as caught:
            await ask(LiteLLMProvider("anthropic", KEY))
        assert caught.value.sent is may_have_been_billed

    @pytest.mark.parametrize(
        "error",
        [
            litellm.AuthenticationError(message="a", llm_provider="anthropic", model="m"),
            litellm.BadRequestError(message="b", model="m", llm_provider="anthropic"),
            litellm.NotFoundError(message="n", model="m", llm_provider="anthropic"),
        ],
    )
    async def test_litellms_refusals_are_not_retried(self, stand: Stand, error: Exception) -> None:
        stand.outcome = error
        with pytest.raises(ProviderRejected):
            await ask(LiteLLMProvider("anthropic", KEY))

    async def test_a_missing_library_is_a_configuration_error_before_anything_is_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def missing() -> Any:
            raise ImportError("no litellm")

        monkeypatch.setattr(litellm_provider, "_quiet_litellm", missing)
        with pytest.raises(LLMNotConfigured):
            await ask(LiteLLMProvider("anthropic", KEY))

    async def test_an_answer_that_never_comes_is_given_up_on(self, stand: Stand) -> None:
        async def never() -> Any:
            await asyncio.sleep(3600)

        stand.outcome = never
        with pytest.raises(ProviderFailed) as caught:
            await ask(LiteLLMProvider("anthropic", KEY), deadline_seconds=0.01)
        assert caught.value.sent is True  # the request may have been billed: the booking stays

    @pytest.mark.parametrize(
        "outcome", [types.SimpleNamespace(choices=[]), types.SimpleNamespace(), None]
    )
    async def test_an_answer_of_a_shape_nobody_expected_is_a_failure(
        self, stand: Stand, outcome: Any
    ) -> None:
        stand.outcome = outcome if outcome is not None else types.SimpleNamespace(choices=None)
        with pytest.raises(ProviderFailed):
            await ask(LiteLLMProvider("anthropic", KEY))

    async def test_no_text_is_an_empty_answer_for_the_gateway_to_refuse(self, stand: Stand) -> None:
        stand.outcome = response(None)
        assert (await ask(LiteLLMProvider("anthropic", KEY))).text == ""

    async def test_the_error_does_not_carry_the_providers_message_or_the_key(
        self, stand: Stand
    ) -> None:
        stand.outcome = StatusError(401)
        with pytest.raises(ProviderRejected) as caught:
            await ask(LiteLLMProvider("anthropic", KEY))
        assert "KEY-SENTINEL" not in str(caught.value) and "provider said no" not in str(
            caught.value
        )
        assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_litellm_is_made_quiet_before_it_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real module: every switch that keeps it from downloading configuration is forced on,
    even if the environment says otherwise, and its message logging and callbacks are off."""
    # Written out here, apart from the code under test: a switch that is dropped from the list
    # must fail, and a new LiteLLM that adds one fails the no-socket test below until it is added.
    required = {
        "LITELLM_LOCAL_MODEL_COST_MAP",
        "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS",
        "LITELLM_LOCAL_BLOG_POSTS",
        "LITELLM_LOCAL_AUTOROUTER_PRESETS",
    }
    for name in required:
        monkeypatch.setenv(name, "False")
    module = litellm_provider._quiet_litellm()
    for name in required:
        assert os.environ[name] == "True", name
    assert module.turn_off_message_logging is True and module.drop_params is True
    assert not module.success_callback and not module.failure_callback and not module.callbacks


@pytest.mark.filterwarnings("ignore::UserWarning")  # LiteLLM's own types warn when first used
async def test_the_real_library_opens_no_connection_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LiteLLM's own mock answer exercises the real code path of a call. No socket may be opened
    to anywhere: not the provider (mocked), and above all not GitHub for its price list or its
    header table. A new LiteLLM that fetches something by itself fails this test."""
    attempts: list[object] = []

    def refuse(self: socket.socket, address: object) -> None:
        attempts.append(address)
        raise OSError("no network in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(
        socket.socket, "connect_ex", lambda self, address: attempts.append(address) or 1
    )
    module = litellm_provider._quiet_litellm()
    result = await module.acompletion(
        model="anthropic/claude-test",
        messages=[{"role": "user", "content": "hello"}],
        mock_response='{"summary": "x", "score": 1}',
        api_key="k",
        max_tokens=10,
        num_retries=0,
    )
    assert result.choices[0].message.content == '{"summary": "x", "score": 1}'
    assert attempts == []


def test_nothing_is_fetched_when_litellm_is_imported_and_used_in_a_fresh_process() -> None:
    """The in-process test cannot see what happens at import (the module is already loaded). A new
    interpreter blocks every socket first, then imports and uses LiteLLM through the provider."""
    program = textwrap.dedent(
        """
        import asyncio, socket, warnings
        attempts = []
        def refuse(self, address):
            attempts.append(address)
            raise OSError("no network")
        socket.socket.connect = refuse
        socket.socket.connect_ex = lambda self, address: attempts.append(address) or 1
        warnings.simplefilter("ignore")
        from pharos.llm import litellm_provider
        module = litellm_provider._quiet_litellm()
        result = asyncio.run(module.acompletion(
            model="anthropic/claude-test", messages=[{"role": "user", "content": "hi"}],
            mock_response="{}", api_key="k", max_tokens=5, num_retries=0))
        assert result.choices[0].message.content == "{}"
        print("ATTEMPTS", len(attempts))
        """
    )
    done = subprocess.run(  # noqa: S603 (our own interpreter, a fixed program)
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "ATTEMPTS 0" in done.stdout
