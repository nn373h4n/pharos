"""Every base image and build tool is pinned by digest or hash (F-05, ADR-0024).

A floating tag (`python:3.12-slim`) means two builds of one commit can differ, and a tool fetched
from an index at build time can change under us. The release process depends on both being fixed.
The images Compose builds itself (`pharos-backend`, `pharos-web`) are the only exceptions.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
BUILT_HERE = re.compile(r"^pharos-(backend|web):\$\{PHAROS_IMAGE_TAG:-local\}$")
SKIP_DIRS = {"node_modules", ".next", ".venv", ".git", ".claude"}


def _files(pattern: str) -> list[Path]:
    return sorted(p for p in ROOT.rglob(pattern) if not SKIP_DIRS & set(p.relative_to(ROOT).parts))


def unpinned_from_lines(text: str) -> list[str]:
    """`FROM` references that are neither a digest-pinned image nor an earlier build stage."""
    stages: set[str] = set()
    bad: list[str] = []
    for raw in text.splitlines():
        match = re.match(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", raw.strip(), re.IGNORECASE)
        if not match:
            continue
        image, stage = match.groups()
        if image not in stages and not DIGEST.search(image):
            bad.append(image)
        if stage:
            stages.add(stage)
    return bad


def unpinned_compose_images(text: str) -> list[str]:
    """`image:` values that are neither digest-pinned nor an image this repository builds."""
    values = re.findall(r"^\s*image:\s*(\S+)\s*$", text, re.MULTILINE)
    return [v for v in values if not DIGEST.search(v) and not BUILT_HERE.match(v)]


DIGEST_OK = "a" * 64


def test_checker_accepts_pinned_and_rejects_floating_dockerfile_lines() -> None:
    pinned = f"FROM python:3.12-slim@sha256:{DIGEST_OK} AS build\nFROM build\n"
    assert unpinned_from_lines(pinned) == []
    # Positive controls: the checker must bite.
    assert unpinned_from_lines("FROM python:3.12-slim AS build\n") == ["python:3.12-slim"]
    assert unpinned_from_lines(f"FROM x@sha256:{'a' * 63}\n") == [f"x@sha256:{'a' * 63}"]
    assert unpinned_from_lines("FROM build\n") == ["build"]  # a stage that was never declared


def test_checker_accepts_pinned_and_rejects_floating_compose_images() -> None:
    ok = (
        f"    image: caddy:2-alpine@sha256:{DIGEST_OK}\n"
        "    image: pharos-web:${PHAROS_IMAGE_TAG:-local}\n"
    )
    assert unpinned_compose_images(ok) == []
    assert unpinned_compose_images("    image: redis:8.10.0-alpine\n") == ["redis:8.10.0-alpine"]
    assert unpinned_compose_images("    image: pharos-web:latest\n") == ["pharos-web:latest"]


def test_the_scan_finds_the_files_it_is_meant_to_check() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in _files("Dockerfile*")}
    assert {"backend/Dockerfile", "frontend/Dockerfile"} <= names
    compose = {p.relative_to(ROOT).as_posix() for p in _files("docker-compose*.yml")}
    assert {"deploy/docker-compose.yml", "deploy/docker-compose.sso-dev.yml"} <= compose


@pytest.mark.parametrize(
    "path", _files("Dockerfile*"), ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_every_dockerfile_base_image_is_pinned_by_digest(path: Path) -> None:
    assert unpinned_from_lines(path.read_text()) == []


@pytest.mark.parametrize(
    "path", _files("docker-compose*.yml"), ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_every_compose_image_is_pinned_by_digest_or_built_here(path: Path) -> None:
    assert unpinned_compose_images(path.read_text()) == []


def test_the_backend_image_does_not_install_tools_from_an_index() -> None:
    text = (ROOT / "backend/Dockerfile").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "pip install" not in code and "pip3 install" not in code


def test_uv_in_the_image_is_the_version_ci_uses() -> None:
    dockerfile = (ROOT / "backend/Dockerfile").read_text()
    in_image = re.search(r"^FROM ghcr\.io/astral-sh/uv:([\d.]+)@sha256:", dockerfile, re.MULTILINE)
    in_ci = re.search(
        r'^\s*UV_VERSION:\s*"([\d.]+)"',
        (ROOT / ".github/workflows/ci.yml").read_text(),
        re.MULTILINE,
    )
    assert in_image and in_ci
    assert in_image.group(1) == in_ci.group(1)


def test_pnpm_is_pinned_with_a_hash() -> None:
    text = (ROOT / "frontend/package.json").read_text()
    assert re.search(r'"packageManager":\s*"pnpm@[\d.]+\+sha512\.[0-9a-f]{128}"', text)
