"""Building the gateway from the settings, in the worker.

The key lives in the worker's environment only (the API and the scheduler never hold it), so a
provider named without a key is not an error of the settings: the process that makes no calls
cannot tell. It is an error here, where a call is about to be possible.
"""

from pydantic import SecretStr
from redis.asyncio import Redis

from pharos.core.clock import Clock
from pharos.core.config import Settings
from pharos.llm.budget import Budget
from pharos.llm.errors import LLMNotConfigured
from pharos.llm.gateway import Event, GatewayConfig, LLMGateway
from pharos.llm.litellm_provider import LiteLLMProvider, local_model_url
from pharos.llm.provider import DemoProvider, Provider
from pharos.llm.types import Stage
from pharos.llm.usage import UsageSink


def build_provider(settings: Settings) -> Provider | None:
    """The provider named in the settings; None when the model layer is off."""
    name = settings.llm_provider
    if name == "off":
        return None
    if name == "fake":
        return DemoProvider()
    if name == "ollama":
        # No key to give; the address is the fixed IP of the server on the internal network.
        return LiteLLMProvider(
            "ollama", SecretStr("ollama"), base_url=local_model_url(settings.ollama_ip)
        )
    if settings.llm_api_key is None:
        raise LLMNotConfigured(f"llm_provider {name} needs llm_api_key in the worker")
    return LiteLLMProvider(name, settings.llm_api_key, base_url=settings.llm_base_url)


def gateway_config(settings: Settings) -> GatewayConfig:
    local = settings.llm_provider == "ollama"  # a local model costs nothing per token
    return GatewayConfig(
        models={
            Stage.FILTER: settings.llm_model_filter,
            Stage.ENRICHMENT: settings.llm_model_enrichment,
            Stage.INSIGHT: settings.llm_model_insight,
            Stage.KEY_TEST: settings.llm_model_filter,
        },
        calls_per_minute=settings.llm_calls_per_minute,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        price_in=0 if local else settings.llm_price_in_micro_usd,
        price_out=0 if local else settings.llm_price_out_micro_usd,
    )


def build_gateway(
    settings: Settings,
    redis: Redis,
    usage: UsageSink,
    clock: Clock,
    *,
    on_event: Event | None = None,
) -> LLMGateway:
    budget = Budget(redis, clock, daily_cap_micro_usd=settings.llm_daily_budget_micro_usd)
    return LLMGateway(
        build_provider(settings), budget, usage, gateway_config(settings), clock, on_event=on_event
    )
