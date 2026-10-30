"""Proves that the import-linter contracts really fail on a violation (ADR-0001)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parents[1] / ".importlinter"
RUN = "from importlinter.cli import lint_imports_command; lint_imports_command()"


def _lint(project: Path) -> subprocess.CompletedProcess[str]:
    # The fixture package is also called `pharos`; PYTHONPATH makes it win over the real one.
    env = {**os.environ, "PYTHONPATH": str(project)}
    return subprocess.run(  # noqa: S603 - fixed arguments, no untrusted input
        [sys.executable, "-c", RUN, "--config", str(project / ".importlinter")],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _project(root: Path, files: dict[str, str]) -> Path:
    shutil.copy(CONFIG, root / ".importlinter")
    for name in ("", "orgs", "ingest", "sources", "items", "feeds"):
        package = root / "pharos" / name
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("")
    for relative, content in files.items():
        (root / "pharos" / "orgs" / relative).write_text(content)
    return root


def test_clean_modules_pass(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        {
            "repository.py": "VALUE = 1\n",
            "service.py": "from pharos.orgs import repository\n",
            "router.py": "from fastapi import APIRouter\nfrom pharos.orgs import service\n",
        },
    )
    result = _lint(project)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("framework", ["fastapi", "starlette"])
def test_service_importing_the_web_framework_is_rejected(tmp_path: Path, framework: str) -> None:
    project = _project(tmp_path, {"service.py": f"import {framework}\n"})
    result = _lint(project)
    assert result.returncode != 0
    assert "Services do not import FastAPI BROKEN" in result.stdout


def test_router_importing_a_repository_is_rejected(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        {"repository.py": "VALUE = 1\n", "router.py": "from pharos.orgs import repository\n"},
    )
    result = _lint(project)
    assert result.returncode != 0
    assert "Routers go through services, never repositories BROKEN" in result.stdout


def test_only_the_identity_provider_client_may_import_httpx(tmp_path: Path) -> None:
    project = _project(tmp_path, {"service.py": "import httpx\n"})
    result = _lint(project)
    assert result.returncode != 0
    assert "Only two modules import httpx (ADR-0011, ADR-0012) BROKEN" in result.stdout


def test_the_identity_provider_client_itself_is_allowed_to_import_httpx(tmp_path: Path) -> None:
    project = _project(tmp_path, {})
    client = project / "pharos" / "identity" / "oidc"
    client.mkdir(parents=True)
    for package in (project / "pharos" / "identity", client):
        (package / "__init__.py").write_text("")
    (client / "client.py").write_text("import httpx\n")
    result = _lint(project)
    assert result.returncode == 0, result.stdout + result.stderr


def test_nothing_else_may_import_httpcore(tmp_path: Path) -> None:
    project = _project(tmp_path, {"service.py": "import httpcore\n"})
    result = _lint(project)
    assert result.returncode != 0
    assert "not allowed to import httpcore" in result.stdout


def _with_net(project: Path) -> None:
    net = project / "pharos" / "net"
    net.mkdir(parents=True)
    (net / "__init__.py").write_text("")
    (net / "safe_http.py").write_text("import httpx\nimport httpcore\n")


def test_safe_http_client_itself_is_allowed_to_import_httpx_and_httpcore(tmp_path: Path) -> None:
    project = _project(tmp_path, {})
    _with_net(project)
    result = _lint(project)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("module", ["router.py", "service.py"])
def test_api_modules_may_not_import_the_fetcher(tmp_path: Path, module: str) -> None:
    project = _project(tmp_path, {module: "from pharos.net import safe_http\n"})
    _with_net(project)
    result = _lint(project)
    assert result.returncode != 0
    assert "API modules never import the fetcher" in result.stdout


def _with_ingest(project: Path, source: str) -> None:
    (project / "pharos" / "ingest" / "connector.py").write_text(source)


@pytest.mark.parametrize(
    "statement",
    [
        "import socket",
        "import requests",
        "import urllib.request",
        "from urllib import request",
        "import http.client",
        "import aiohttp",
        "import urllib3",
        "import httpx",
        "import ftplib",
    ],
)
def test_ingestion_code_may_not_open_connections_itself(tmp_path: Path, statement: str) -> None:
    project = _project(tmp_path, {})
    _with_ingest(project, statement + "\n")
    result = _lint(project)
    assert result.returncode != 0, statement
    assert "BROKEN" in result.stdout


def test_ingestion_code_may_use_the_safe_client_and_parse_text(tmp_path: Path) -> None:
    project = _project(tmp_path, {})
    _with_net(project)
    _with_ingest(project, "from pharos.net import safe_http\nimport re\nimport hashlib\n")
    result = _lint(project)
    assert result.returncode == 0, result.stdout + result.stderr


# --- the sources API can validate a configuration but never load anything that fetches (M3-10) ---

SOURCES_RULE = "The sources API never loads anything that fetches (M3-10)"


def _with_sources(project: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        (project / "pharos" / "sources" / relative).write_text(content)


def _with_ingest_modules(project: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = project / "pharos" / "ingest" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if path.name != "__init__.py" and path.parent.name != "ingest":
            (path.parent / "__init__.py").write_text("")


def test_the_sources_api_may_use_the_network_free_configuration_module(tmp_path: Path) -> None:
    project = _project(tmp_path, {})
    _with_ingest_modules(project, {"sourceconfig.py": "import re\nimport hashlib\n"})
    _with_sources(project, {"service.py": "from pharos.ingest import sourceconfig\n"})
    result = _lint(project)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("module", ["service.py", "repository.py", "schemas.py"])
def test_the_sources_api_may_not_import_the_http_client_directly(
    tmp_path: Path, module: str
) -> None:
    project = _project(tmp_path, {})
    _with_net(project)
    _with_sources(project, {module: "from pharos.net import safe_http\n"})
    result = _lint(project)
    assert result.returncode != 0
    assert f"{SOURCES_RULE} BROKEN" in result.stdout


def test_the_sources_api_may_not_reach_the_http_client_through_another_module(
    tmp_path: Path,
) -> None:
    # The older rule allows indirect imports, so this is the one only the new rule catches.
    project = _project(tmp_path, {})
    _with_net(project)
    _with_ingest_modules(project, {"helper.py": "from pharos.net import safe_http\n"})
    _with_sources(project, {"service.py": "from pharos.ingest import helper\n"})
    result = _lint(project)
    assert result.returncode != 0
    assert f"{SOURCES_RULE} BROKEN" in result.stdout
    assert "API modules never import the fetcher (ADR-0012) KEPT" in result.stdout


@pytest.mark.parametrize(
    ("files", "statement"),
    [
        ({"fetcher.py": "VALUE = 1\n"}, "from pharos.ingest import fetcher"),
        ({"connectors/__init__.py": "VALUE = 1\n"}, "from pharos.ingest import connectors"),
        ({"connectors/rss.py": "VALUE = 1\n"}, "from pharos.ingest.connectors import rss"),
    ],
)
def test_the_sources_api_may_not_import_the_fetcher_or_the_connectors(
    tmp_path: Path, files: dict[str, str], statement: str
) -> None:
    project = _project(tmp_path, {})
    _with_ingest_modules(project, files)
    _with_sources(project, {"service.py": statement + "\n"})
    result = _lint(project)
    assert result.returncode != 0
    assert f"{SOURCES_RULE} BROKEN" in result.stdout


def test_the_sources_api_may_not_import_the_workers(tmp_path: Path) -> None:
    project = _project(tmp_path, {})
    workers = project / "pharos" / "workers"
    workers.mkdir(parents=True)
    (workers / "__init__.py").write_text("")
    _with_sources(project, {"service.py": "from pharos import workers\n"})
    result = _lint(project)
    assert result.returncode != 0
    assert f"{SOURCES_RULE} BROKEN" in result.stdout


@pytest.mark.parametrize("library", ["httpx", "httpcore"])
def test_the_sources_api_may_not_import_an_http_library(tmp_path: Path, library: str) -> None:
    project = _project(tmp_path, {})
    _with_sources(project, {"service.py": f"import {library}\n"})
    result = _lint(project)
    assert result.returncode != 0
    assert f"{SOURCES_RULE} BROKEN" in result.stdout


def test_the_sources_api_may_not_import_the_http_client_even_if_that_imports_nothing_else(
    tmp_path: Path,
) -> None:
    # Only the explicit entry for the client catches this one; the library entries do not apply.
    project = _project(tmp_path, {})
    net = project / "pharos" / "net"
    net.mkdir(parents=True)
    (net / "__init__.py").write_text("")
    (net / "safe_http.py").write_text("VALUE = 1\n")
    _with_sources(project, {"service.py": "from pharos.net import safe_http\n"})
    result = _lint(project)
    assert result.returncode != 0
    assert f"{SOURCES_RULE} BROKEN" in result.stdout


@pytest.mark.parametrize(
    "statement",
    [
        "import socket",
        "import requests",
        "import urllib.request",
        "from urllib import request",
        "import http.client",
        "import aiohttp",
        "import urllib3",
        "import ftplib",
    ],
)
def test_the_sources_api_may_not_open_a_connection_itself(tmp_path: Path, statement: str) -> None:
    project = _project(tmp_path, {})
    _with_sources(project, {"service.py": statement + "\n"})
    result = _lint(project)
    assert result.returncode != 0, statement
    assert "The sources API opens no connections itself (M3-10) BROKEN" in result.stdout


# --- the items API is under the same two rules as the sources API (M3-10b) ---


def _with_items(project: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        (project / "pharos" / "items" / relative).write_text(content)


def test_the_items_api_may_not_reach_the_http_client_even_through_another_module(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, {})
    _with_net(project)
    _with_ingest_modules(project, {"helper.py": "from pharos.net import safe_http\n"})
    _with_items(project, {"service.py": "from pharos.ingest import helper\n"})
    result = _lint(project)
    assert result.returncode != 0
    assert f"{SOURCES_RULE} BROKEN" in result.stdout


@pytest.mark.parametrize("statement", ["import socket", "import urllib.request", "import requests"])
def test_the_items_api_may_not_open_a_connection_itself(tmp_path: Path, statement: str) -> None:
    project = _project(tmp_path, {})
    _with_items(project, {"service.py": statement + "\n"})
    result = _lint(project)
    assert result.returncode != 0, statement
    assert "The sources API opens no connections itself (M3-10) BROKEN" in result.stdout


def test_the_items_api_may_import_what_it_needs(tmp_path: Path) -> None:
    project = _project(tmp_path, {})
    _with_items(project, {"service.py": "import re\nimport uuid\n"})
    result = _lint(project)
    assert result.returncode == 0, result.stdout + result.stderr


# --- the sources and items APIs do not send tasks themselves (M3-10c) ---


@pytest.mark.parametrize(
    "statement", ["import celery", "from celery import Celery", "import kombu"]
)
@pytest.mark.parametrize("package", ["sources", "items", "feeds"])
def test_the_apis_may_not_use_the_task_queue_libraries_directly(
    tmp_path: Path, statement: str, package: str
) -> None:
    project = _project(tmp_path, {})
    (project / "pharos" / package / "service.py").write_text(statement + "\n")
    result = _lint(project)
    assert result.returncode != 0, statement
    assert "The sources API opens no connections itself (M3-10) BROKEN" in result.stdout


@pytest.mark.parametrize("package", ["sources", "items", "feeds"])
def test_the_apis_get_a_queue_handed_to_them_and_may_not_import_the_queue_client(
    tmp_path: Path, package: str
) -> None:
    project = _project(tmp_path, {})
    core = project / "pharos" / "core"
    core.mkdir(parents=True)
    (core / "__init__.py").write_text("")
    (core / "queue.py").write_text("VALUE = 1\n")
    (project / "pharos" / package / "service.py").write_text("from pharos.core import queue\n")
    result = _lint(project)
    assert result.returncode != 0
    assert f"{SOURCES_RULE} BROKEN" in result.stdout


# --- the personal feeds API is under the same rules (M3-13) ---


def test_the_feeds_api_may_not_reach_the_http_client_even_through_another_module(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, {})
    _with_net(project)
    _with_ingest_modules(project, {"helper.py": "from pharos.net import safe_http\n"})
    (project / "pharos" / "feeds" / "service.py").write_text("from pharos.ingest import helper\n")
    result = _lint(project)
    assert result.returncode != 0
    assert f"{SOURCES_RULE} BROKEN" in result.stdout
