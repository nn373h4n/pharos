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
    for name in ("", "orgs", "ingest"):
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
