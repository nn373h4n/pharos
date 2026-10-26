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
    for name in ("", "orgs"):
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
