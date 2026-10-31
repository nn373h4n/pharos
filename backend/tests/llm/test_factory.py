"""Building the gateway from the settings: the worker holds the key, nobody else needs it."""

import pytest
from redis.asyncio import Redis

from pharos.core.config import Settings
from pharos.llm.errors import LLMDisabled, LLMNotConfigured
from pharos.llm.factory import build_gateway, build_provider, gateway_config
from pharos.llm.litellm_provider import LiteLLMProvider
from pharos.llm.provider import FakeProvider
from pharos.llm.types import Stage, Task

from .support import ORG, Answer, FixedClock, MemoryUsage, article


def settings(**changes: object) -> Settings:
    return Settings(environment="test", **changes)  # type: ignore[arg-type]


def test_it_is_off_unless_a_provider_is_named() -> None:
    assert build_provider(settings()) is None


def test_the_fake_provider_serves_tests_and_demos() -> None:
    assert isinstance(build_provider(settings(llm_provider="fake")), FakeProvider)


@pytest.mark.parametrize("name", ["anthropic", "openai", "azure"])
def test_a_real_provider_is_built_with_its_key(name: str) -> None:
    provider = build_provider(settings(llm_provider=name, llm_api_key="KEY-SENTINEL"))
    assert isinstance(provider, LiteLLMProvider) and provider.name == name
    assert "KEY-SENTINEL" not in repr(provider)


@pytest.mark.parametrize("name", ["anthropic", "openai", "azure"])
def test_a_real_provider_without_a_key_fails_where_a_call_is_about_to_be_possible(
    name: str,
) -> None:
    with pytest.raises(LLMNotConfigured, match="llm_api_key"):
        build_provider(settings(llm_provider=name))


def test_the_models_prices_and_limits_come_from_the_settings() -> None:
    config = gateway_config(
        settings(
            llm_model_filter="small",
            llm_model_enrichment="big",
            llm_model_insight="bigger",
            llm_price_in_micro_usd=75,
            llm_price_out_micro_usd=375,
            llm_calls_per_minute=7,
            llm_timeout_seconds=12,
            llm_max_retries=1,
        )
    )
    assert config.models[Stage.FILTER] == "small" and config.models[Stage.ENRICHMENT] == "big"
    assert config.models[Stage.INSIGHT] == "bigger"
    assert (config.price_in, config.price_out) == (75, 375)
    assert (config.calls_per_minute, config.timeout_seconds, config.max_retries) == (7, 12, 1)


@pytest.mark.anyio
async def test_a_gateway_without_a_provider_refuses_every_call() -> None:
    gateway = build_gateway(
        settings(), Redis.from_url("redis://127.0.0.1:1/0"), MemoryUsage(), FixedClock()
    )
    with pytest.raises(LLMDisabled):
        await gateway.generate(task=Task.ENRICH, org_id=ORG, article=article(), schema=Answer)
