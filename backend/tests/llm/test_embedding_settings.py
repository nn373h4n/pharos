"""The embedding settings and how the embedder and the gateway are built from them (M4-05)."""

import pytest

from pharos.core.config import Settings
from pharos.llm.embedding import DemoEmbedder
from pharos.llm.errors import LLMNotConfigured
from pharos.llm.factory import build_embedder, gateway_config
from pharos.llm.litellm_provider import LiteLLMProvider
from pharos.llm.types import Stage


def settings(**changes: object) -> Settings:
    return Settings(environment="test", **changes)  # type: ignore[arg-type]


def test_embeddings_are_off_by_default_and_nothing_names_a_model() -> None:
    found = settings()
    assert found.embedding_provider == "off" and found.embedding_model == ""
    assert build_embedder(found) is None
    assert gateway_config(found).models[Stage.EMBEDDING] == ""  # ADR-0007: no model in code


def test_the_fake_embedder_is_refused_in_production_for_every_process() -> None:
    with pytest.raises(ValueError, match="embedding"):
        Settings(environment="production", embedding_provider="fake")  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["openai", "azure"])
def test_a_remote_embedder_needs_the_same_chat_provider_for_its_key(name: str) -> None:
    with pytest.raises(ValueError, match="chat provider"):
        settings(embedding_provider=name)
    with pytest.raises(ValueError, match="chat provider"):
        settings(embedding_provider=name, llm_provider="anthropic")
    assert settings(embedding_provider=name, llm_provider=name).embedding_provider == name


@pytest.mark.parametrize("value", [0.49, 1.0, 1.5, -1])
def test_the_similarity_has_sane_bounds(value: float) -> None:
    with pytest.raises(ValueError, match="embedding_similarity"):
        settings(embedding_similarity=value)


def test_the_embedders_built_from_the_settings() -> None:
    assert isinstance(build_embedder(settings(embedding_provider="fake")), DemoEmbedder)
    local = build_embedder(settings(embedding_provider="ollama"))
    assert isinstance(local, LiteLLMProvider) and local.name == "ollama"
    assert local._base_url == "http://172.28.30.10:11434/v1"
    # A remote one is built in the worker with the chat provider's key, and refuses to without.
    with pytest.raises(LLMNotConfigured, match="llm_api_key"):
        build_embedder(settings(embedding_provider="openai", llm_provider="openai"))
    remote = build_embedder(
        settings(embedding_provider="openai", llm_provider="openai", llm_api_key="KEY")
    )
    assert isinstance(remote, LiteLLMProvider) and remote.name == "openai"


def test_the_price_of_an_embedding_is_zero_for_a_local_or_fake_one_and_set_for_a_remote_one() -> (
    None
):
    assert gateway_config(settings(embedding_provider="ollama")).embedding_price == 0
    assert gateway_config(settings(embedding_provider="fake")).embedding_price == 0
    remote = settings(
        embedding_provider="openai", llm_provider="openai", embedding_price_micro_usd=7
    )
    assert gateway_config(remote).embedding_price == 7


def test_the_model_name_reaches_the_embedding_stage() -> None:
    config = gateway_config(settings(embedding_provider="fake", embedding_model="my-embedder"))
    assert config.models[Stage.EMBEDDING] == "my-embedder"


def test_embeddings_have_a_pace_of_their_own() -> None:
    assert gateway_config(settings()).embedding_calls_per_minute == 120
    assert gateway_config(settings(embedding_calls_per_minute=7)).embedding_calls_per_minute == 7


@pytest.mark.parametrize("name", ["a b", "x+y", "-lead", ".lead", "é", "a" * 101, "x\n"])
def test_a_model_name_the_table_would_refuse_is_refused_at_start_up(name: str) -> None:
    """Otherwise every article is billed, fails to be stored and is tried again, until the cap."""
    with pytest.raises(ValueError, match="embedding_model"):
        settings(embedding_model=name)


@pytest.mark.parametrize(
    "name", ["", "nomic-embed-text:latest", "text-embedding-3-small", "org/model@v1.2", "a" * 100]
)
def test_the_usual_model_names_are_accepted(name: str) -> None:
    assert settings(embedding_model=name).embedding_model == name
