"""The built-in connectors. There is no plug-in mechanism: a connector is code in this repo."""

from pharos.ingest.connectors.base import (
    ConfigError,
    Connector,
    ConnectorError,
    FetchOutcome,
    FetchState,
)
from pharos.ingest.connectors.rss import RssConnector

CONNECTORS: dict[str, Connector] = {"rss": RssConnector()}

__all__ = [
    "CONNECTORS",
    "ConfigError",
    "Connector",
    "ConnectorError",
    "FetchOutcome",
    "FetchState",
    "RssConnector",
]
