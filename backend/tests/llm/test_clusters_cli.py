"""`python -m pharos.llm.clusters status`: what it prints about vectors of another model (F-42)."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from pharos.llm import clusters

pytestmark = pytest.mark.anyio


class Engine:
    async def dispose(self) -> None:
        return None


class Resolver:
    def __init__(self, *_: Any) -> None:
        pass

    async def resolve(self) -> uuid.UUID:
        return uuid.UUID(int=7)


def stub(monkeypatch: pytest.MonkeyPatch, model: str, other: int | None) -> list[str | None]:
    asked: list[str | None] = []

    async def status(engine: Any, org: uuid.UUID, *, model: str | None = None) -> dict[str, int]:
        asked.append(model)
        counts = {"vectors": 9, "clustered": 4, "clusters": 2, "largest": 2}
        if model is not None and other is not None:
            counts["other_models"] = other
        return counts

    settings = SimpleNamespace(
        database_url=SimpleNamespace(get_secret_value=lambda: "postgresql+asyncpg://x"),
        org_slug="acme",
        embedding_model=model,
    )
    monkeypatch.setattr(clusters, "get_settings", lambda: settings)
    monkeypatch.setattr(clusters, "create_engine", lambda url: Engine())
    monkeypatch.setattr(clusters, "OrganizationResolver", Resolver)
    monkeypatch.setattr(clusters.clustering, "status", status)
    return asked


async def test_the_report_names_the_vectors_of_another_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = stub(monkeypatch, "new-model", 5)
    out = await clusters.execute(clusters.build_parser().parse_args(["status", "--report"]))
    assert out.splitlines() == ["vectors=9", "clusters=2", "other_model_vectors=5"]
    assert asked == ["new-model"]  # compared with the configured model


async def test_with_embeddings_off_the_report_says_off(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = stub(monkeypatch, "", None)
    out = await clusters.execute(clusters.build_parser().parse_args(["status", "--report"]))
    assert "other_model_vectors=off" in out.splitlines()
    assert asked == [None]


async def test_the_plain_status_tells_the_operator_to_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub(monkeypatch, "new-model", 5)
    out = await clusters.execute(clusters.build_parser().parse_args(["status"]))
    assert out.startswith("9 vector(s); 4 article(s) in 2 cluster(s)")
    assert "5 vector(s) of another model" in out and "reset --confirm" in out
    stub(monkeypatch, "new-model", 0)
    quiet = await clusters.execute(clusters.build_parser().parse_args(["status"]))
    assert "another model" not in quiet and len(quiet.splitlines()) == 1
