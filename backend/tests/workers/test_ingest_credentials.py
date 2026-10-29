"""What the fetch task hands to the connectors (M3-09): the GitHub credential and the lease."""

import asyncio
import uuid

import pytest
from pydantic import SecretStr

from pharos.core.config import Settings
from pharos.ingest.fetcher import FetchReport
from pharos.workers import ingest_tasks

GITHUB_TOKEN = "github_pat_11AAAAAAA0abcdefghijkl"


class FakeRedis:
    async def aclose(self) -> None:
        pass


def run_fetch_task(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> dict[str, object]:
    seen: dict[str, object] = {}

    async def fake_fetch_source(engine: object, **kwargs: object) -> FetchReport:
        seen.update(kwargs)
        return FetchReport("ok")

    monkeypatch.setattr(ingest_tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(ingest_tasks.aioredis, "from_url", lambda *_a, **_k: FakeRedis())
    monkeypatch.setattr(ingest_tasks, "fetch_source", fake_fetch_source)
    monkeypatch.setattr(ingest_tasks.runtime, "run", lambda work: asyncio.run(work(None)))  # type: ignore[arg-type]
    source, lease = uuid.uuid4(), uuid.uuid4()
    ingest_tasks.fetch_task.run(str(source), str(lease))
    seen["expected"] = (source, lease)
    return seen


def test_the_credential_reaches_the_github_connector_and_the_lease_is_the_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(environment="test", github_token=SecretStr(GITHUB_TOKEN))  # type: ignore[arg-type]
    seen = run_fetch_task(monkeypatch, settings)
    assert (seen["source_id"], seen["lease_token"]) == seen["expected"]  # not the GitHub credential
    connectors = seen["connectors"]
    assert connectors["github"]._token == GITHUB_TOKEN  # type: ignore[index,attr-defined]
    assert set(connectors) == {"rss", "github", "hackernews"}  # type: ignore[arg-type]
    assert GITHUB_TOKEN not in repr(connectors)


def test_without_a_credential_the_github_connector_is_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = run_fetch_task(monkeypatch, Settings(environment="test"))  # type: ignore[arg-type]
    assert seen["connectors"]["github"]._token is None  # type: ignore[index,attr-defined]
