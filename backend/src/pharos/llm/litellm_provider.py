"""The only module that imports LiteLLM (ADR-0002, ADR-0015 point 9).

LiteLLM makes its own HTTPS calls to the provider's endpoint, which an administrator chooses and
an article never does, so these calls do not go through `SafeHttpClient`. Everything else in the
product reaches the network only through it. Before LiteLLM is imported, every switch that stops it
from fetching configuration from the internet by itself is forced on (not merely defaulted, so an
environment cannot turn it back off): its price list, its Anthropic beta-header table, its blog
posts and its router presets are all read from the copies bundled with the package. A test calls
the real library with its mock response and refuses every network connection.
"""

import asyncio
import os
from typing import Any

from pydantic import SecretStr

from pharos.llm.errors import LLMNotConfigured, ProviderFailed, ProviderRejected
from pharos.llm.provider import ProviderAnswer

# Providers LiteLLM is asked to use, written as its model prefix. Anthropic first (the maintainer's
# decision of 2026-10-31); others are a line here and a configuration value, not new code.
PREFIXES = {"anthropic": "anthropic", "openai": "openai", "azure": "azure"}

# Every switch of LiteLLM 1.105.1 that keeps it from downloading configuration. A new version may
# add one: the test that blocks sockets fails then, and the list grows.
OFFLINE = (
    "LITELLM_LOCAL_MODEL_COST_MAP",
    "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS",
    "LITELLM_LOCAL_BLOG_POSTS",
    "LITELLM_LOCAL_AUTOROUTER_PRESETS",
)


def _quiet_litellm() -> Any:
    for name in OFFLINE:
        os.environ[name] = "True"
    os.environ["LITELLM_LOG"] = "ERROR"
    import litellm

    litellm.turn_off_message_logging = True
    litellm.drop_params = True
    litellm.success_callback = []
    litellm.failure_callback = []
    litellm.callbacks = []
    return litellm


class LiteLLMProvider:
    def __init__(self, provider: str, api_key: SecretStr, *, base_url: str | None = None) -> None:
        if provider not in PREFIXES:
            raise ValueError(f"unknown provider {provider!r}")
        self.name = provider
        self._prefix = PREFIXES[provider]
        self._api_key = api_key
        self._base_url = base_url
        self._litellm: Any = None

    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int, deadline_seconds: float
    ) -> ProviderAnswer:
        if self._litellm is None:
            try:
                self._litellm = _quiet_litellm()
            except ImportError:
                raise LLMNotConfigured("LiteLLM is not installed") from None
        litellm = self._litellm
        arguments: dict[str, Any] = {
            "model": f"{self._prefix}/{model}",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
            "timeout": deadline_seconds,
            "num_retries": 0,  # the gateway decides about retries, and counts them in the budget
            "api_key": self._api_key.get_secret_value(),
            # No tools, ever: the product's model reads text and returns text.
        }
        if self._base_url:
            arguments["api_base"] = self._base_url
        # What went wrong is decided inside the handlers and raised after them, so the error that
        # reaches the caller carries no `__context__`: the original may hold a key fragment.
        failure: Exception | None = None
        response: Any = None
        try:
            response = await asyncio.wait_for(
                litellm.acompletion(**arguments), deadline_seconds + 5
            )
        except TimeoutError:
            failure = ProviderFailed(sent=True)  # the request may have been billed
        except Exception as error:
            status = getattr(error, "status_code", None)
            # LiteLLM gives its own timeout the status 408 and a broken connection 500, though no
            # provider answered: the request may have been billed, so these come first.
            if isinstance(error, (litellm.Timeout, litellm.APIConnectionError)):
                failure = ProviderFailed(sent=True)
            elif status in (400, 401, 403, 404):
                failure = ProviderRejected()  # a wrong key or a refused request
            elif isinstance(status, int):
                failure = ProviderFailed(sent=False)  # the provider answered with an error status
            else:
                failure = ProviderFailed(sent=True)  # a broken connection: it may have been billed
        if failure is not None:
            raise failure
        try:
            text = response.choices[0].message.content or ""
            usage = response.usage
            answer = ProviderAnswer(
                str(text), int(usage.prompt_tokens), int(usage.completion_tokens)
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            answer = None
        if answer is None:
            raise ProviderFailed(sent=True)  # something came back, and was probably billed
        return answer
