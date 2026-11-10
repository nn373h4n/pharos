"""`scripts/check-release-version.sh` stops a tag that differs from the code's version (M5-10d)."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check-release-version.sh"


def tree(tmp_path: Path, backend: str | None, frontend: str | None) -> Path:
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    if backend is not None:
        (tmp_path / "backend/pyproject.toml").write_text(
            f'[project]\nname = "pharos"\nversion = "{backend}"\nrequires-python = ">=3.12"\n'
        )
    if frontend is not None:
        (tmp_path / "frontend/package.json").write_text(
            f'{{\n  "name": "pharos-frontend",\n  "version": "{frontend}",\n  "private": true\n}}\n'
        )
    return tmp_path


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [str(SCRIPT), *args], capture_output=True, text=True, timeout=30
    )


def test_a_matching_tag_passes(tmp_path: Path) -> None:
    result = run("v0.1.0", str(tree(tmp_path, "0.1.0", "0.1.0")))
    assert result.returncode == 0, result.stderr
    assert "matches the tag" in result.stdout


def test_a_prerelease_suffix_is_not_compared(tmp_path: Path) -> None:
    assert run("v0.1.0-rc.1", str(tree(tmp_path, "0.1.0", "0.1.0"))).returncode == 0


@pytest.mark.parametrize(
    ("backend", "frontend", "named"),
    [
        ("0.0.0", "0.1.0", "backend/pyproject.toml"),
        ("0.1.0", "0.0.0", "frontend/package.json"),
        ("0.1.1", "0.1.1", "backend/pyproject.toml"),
        ("0.1.0-dev", "0.1.0", "backend/pyproject.toml"),
    ],
)
def test_a_version_that_differs_stops_the_release_and_names_the_file(
    tmp_path: Path, backend: str, frontend: str, named: str
) -> None:
    result = run("v0.1.0", str(tree(tmp_path, backend, frontend)))
    assert result.returncode == 1
    assert named in result.stderr
    assert "releasing.md" in result.stderr


def test_a_missing_version_fails(tmp_path: Path) -> None:
    result = run("v0.1.0", str(tree(tmp_path, None, "0.1.0")))
    assert result.returncode != 0


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1", "latest", "v0.1.0;x", "v0.1.0\n", "x\nv0.1.0"])
def test_a_tag_that_is_not_a_version_is_refused(tmp_path: Path, tag: str) -> None:
    result = run(tag, str(tree(tmp_path, "0.1.0", "0.1.0")))
    assert result.returncode == 2


def test_wrong_argument_counts_are_refused() -> None:
    assert run().returncode == 2
    assert run("v0.1.0", "a", "b").returncode == 2


def test_the_repository_itself_is_at_a_version_the_script_can_read() -> None:
    """Not a release check: only that both files still have the shape the script reads."""
    for tag in ("v0.0.0",):
        result = run(tag)
        # While the project is at 0.0.0 this passes; once raised it must still be readable.
        assert "says 'nothing'" not in result.stderr
