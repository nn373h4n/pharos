"""The built-in connectors. There is no plug-in mechanism: a connector is code in this repo."""

from pharos.ingest.connectors.base import (
    ConfigError,
    Connector,
    ConnectorError,
    FetchOutcome,
    FetchState,
)
from pharos.ingest.connectors.github import GithubConnector
from pharos.ingest.connectors.hackernews import HackerNewsConnector
from pharos.ingest.connectors.rss import RssConnector


def build_connectors(github_token: str | None = None) -> dict[str, Connector]:
    """The built-in connectors. Only the GitHub one is given a credential, and only when built."""
    return {
        "rss": RssConnector(),
        "github": GithubConnector(github_token),
        "hackernews": HackerNewsConnector(),
    }


CONNECTORS: dict[str, Connector] = (
    build_connectors()
)  # without a credential: for configuration checks

__all__ = [
    "CONNECTORS",
    "ConfigError",
    "Connector",
    "ConnectorError",
    "FetchOutcome",
    "FetchState",
    "GithubConnector",
    "HackerNewsConnector",
    "RssConnector",
    "build_connectors",
]
