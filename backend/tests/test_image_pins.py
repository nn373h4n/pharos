"""Every base image and build tool is pinned by digest or hash (F-05, ADR-0024).

A floating tag (`python:3.12-slim`) means two builds of one commit can differ, and a tool fetched
from an index at build time can change under us. The release process depends on both being fixed.
The images Compose builds itself (`pharos-backend`, `pharos-web`) are the only exceptions.

What is checked: every Dockerfile (`FROM`, `COPY --from`, `RUN --mount=from=`), every `image:` and
`*_IMAGE` key in any YAML file (Compose, workflows), every literal `*_IMAGE=` / `image=` assignment
in scripts, hooks and the Makefile, and every image-shaped argument of a `docker run` line there.
Not checked: an official one-word image (`redis:8`) written directly in a `docker run` line, and
images chosen at run time (`$image`, `$(...)`); today those lines take their image from Compose.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
BUILT_HERE = re.compile(r"^pharos-(backend|web):\$\{PHAROS_IMAGE_TAG:-local\}$")
SKIP_DIRS = {"node_modules", ".next", ".venv", ".git", ".claude", "test-results"}
# `registry/name:tag`, `name/name` or `registry.tld/a/b@sha256:...`: has a slash, so it is not a
# path (those start with `/`, `.` or `$`) and not an option.
IMAGE_SHAPED = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9._-]+)+(?::[\w.-]+)?(?:@sha256:\w+)?$")


def _files(*patterns: str) -> list[Path]:
    found = {
        p
        for pattern in patterns
        for p in ROOT.rglob(pattern)
        if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts)
    }
    return sorted(found)


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _pinned(ref: str) -> bool:
    return bool(DIGEST.search(ref))


def unpinned_dockerfile_refs(text: str) -> list[str]:
    """Image references that are neither digest-pinned nor an earlier build stage."""
    logical: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        logical.append((pending + line).strip())
        pending = ""
    if pending:
        logical.append(pending.strip())

    stages: set[str] = set()
    bad: list[str] = []

    def check(ref: str) -> None:
        if ref.lower() in stages or ref.isdigit() or _pinned(ref):
            return
        bad.append(ref)

    for line in logical:
        words = line.split()
        keyword = words[0].upper() if words else ""
        if keyword == "FROM":
            args = [w for w in words[1:] if not w.startswith("--")]
            if args:
                check(args[0])
            if len(args) >= 3 and args[1].upper() == "AS":
                stages.add(args[2].lower())
        elif keyword in {"COPY", "ADD"}:
            for word in words[1:]:
                if word.startswith("--from="):
                    check(word.removeprefix("--from="))
        elif keyword == "RUN":
            for ref in re.findall(r"(?:^|[\s,=])from=([^,\s]+)", line):
                check(ref)
    return bad


_YAML_IMAGE = re.compile(
    r"""(?:^|[\s{,])(?:image|\w+_IMAGE):\s*["']?((?:\$\{[^}]*\}|[^\s"',}#])+)"""
)


def unpinned_yaml_images(text: str) -> list[str]:
    """Image values that are neither digest-pinned nor an image this repository builds.

    Read line by line, not with a YAML parser (PyYAML is only a transitive dependency): the key
    may sit in a flow mapping (`{image: x}`), the value may be quoted, and a comment may follow.
    """
    values: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        values.extend(_YAML_IMAGE.findall(line))
    return [
        v for v in values if not _pinned(v) and not BUILT_HERE.match(v) and not v.startswith("$")
    ]


_ASSIGN = re.compile(r"""^\s*(?:export\s+)?(\w*IMAGE\w*|image)=["']?([^\s"')]+)""", re.IGNORECASE)


def unpinned_script_images(text: str) -> list[str]:
    """Literal image assignments and image-shaped `docker run` arguments without a digest."""
    bad: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0] if raw.lstrip().startswith("#") else raw
        if line.lstrip().startswith("#"):
            continue
        assign = _ASSIGN.match(line)
        if assign:
            value = assign.group(2)
            if not value.startswith("$") and not _pinned(value):
                bad.append(value)
        if re.search(r"\bdocker\s+(run|create)\b", line):
            for token in line.replace('"', " ").replace("'", " ").split():
                if IMAGE_SHAPED.match(token) and not _pinned(token):
                    bad.append(token)
    return bad


DIGEST_OK = "a" * 64
PINNED = f"python:3.12-slim@sha256:{DIGEST_OK}"


def test_dockerfile_checker_accepts_pinned_lines_and_stage_references() -> None:
    text = f"FROM {PINNED} AS build\nFROM build\nCOPY --from=build /a /b\nCOPY --from=0 /a /b\n"
    assert unpinned_dockerfile_refs(text) == []
    pinned_multiline = f"FROM \\\n  {PINNED} AS x\nRUN --mount=type=bind,from=x,target=/t true\n"
    assert unpinned_dockerfile_refs(pinned_multiline) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FROM python:3.12-slim AS build\n", ["python:3.12-slim"]),
        (f"FROM x@sha256:{'a' * 63}\n", [f"x@sha256:{'a' * 63}"]),
        ("FROM build\n", ["build"]),  # a stage that was never declared
        ("FROM --platform=$BUILDPLATFORM python:3.12-slim AS b\n", ["python:3.12-slim"]),
        ("FROM \\\n  python:3.12-slim\n", ["python:3.12-slim"]),
        (
            f"FROM {PINNED} AS b\nCOPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv\n",
            ["ghcr.io/astral-sh/uv:0.13.5"],
        ),
        (
            f"FROM {PINNED} AS b\nRUN --mount=type=bind,from=ghcr.io/x/y:latest,target=/t true\n",
            ["ghcr.io/x/y:latest"],
        ),
        ("ARG BASE=python:3.12-slim\nFROM ${BASE}\n", ["${BASE}"]),
    ],
)
def test_dockerfile_checker_rejects_every_floating_form(text: str, expected: list[str]) -> None:
    assert unpinned_dockerfile_refs(text) == expected


def test_yaml_checker_accepts_pinned_and_locally_built_images() -> None:
    ok = (
        "services:\n"
        f"  a: {{image: 'caddy:2-alpine@sha256:{DIGEST_OK}'}}\n"
        "  b:\n    image: pharos-web:${PHAROS_IMAGE_TAG:-local}\n"
        f"env:\n  TRIVY_IMAGE: aquasec/trivy@sha256:{DIGEST_OK} # 0.76.1\n"
    )
    assert unpinned_yaml_images(ok) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("services:\n  a:\n    image: redis:8.10.0-alpine\n", ["redis:8.10.0-alpine"]),
        ("services:\n  a:\n    image: redis:8 # pin later\n", ["redis:8"]),
        ("services:\n  a: {image: redis:8}\n", ["redis:8"]),
        ("services:\n  a:\n    image: pharos-web:latest\n", ["pharos-web:latest"]),
        ("jobs:\n  j:\n    container:\n      image: node:24\n", ["node:24"]),
        ("env:\n  K6_IMAGE: grafana/k6:latest\n", ["grafana/k6:latest"]),
    ],
)
def test_yaml_checker_rejects_every_floating_form(text: str, expected: list[str]) -> None:
    assert unpinned_yaml_images(text) == expected


def test_script_checker_accepts_pinned_and_dynamic_images() -> None:
    ok = (
        f"K6_IMAGE=grafana/k6@sha256:{DIGEST_OK}\n"
        'image=$(docker inspect --format "{{.Config.Image}}" "$server")\n'
        'PG_IMAGE=$("$root/scripts/compose-image.sh" postgres)\n'
        'docker run --rm -v "$PWD:/repo:ro" -v cache:/root/.cache "$IMAGE" fs\n'
        f"docker run --rm zricethezav/gitleaks@sha256:{DIGEST_OK} git\n"
        "# K6_IMAGE=grafana/k6:latest is only a comment\n"
    )
    assert unpinned_script_images(ok) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("K6_IMAGE=grafana/k6:latest\n", ["grafana/k6:latest"]),
        ('image="zricethezav/gitleaks:v8.30.1"\n', ["zricethezav/gitleaks:v8.30.1"]),
        ("export TOOL_IMAGE=quay.io/a/b\n", ["quay.io/a/b"]),
        ('docker run --rm -v "$PWD:/w" grafana/k6:latest run x\n', ["grafana/k6:latest"]),
    ],
)
def test_script_checker_rejects_every_floating_form(text: str, expected: list[str]) -> None:
    assert unpinned_script_images(text) == expected


def test_the_scan_finds_the_files_it_is_meant_to_check() -> None:
    dockerfiles = {_rel(p) for p in _DOCKERFILES}
    assert {"backend/Dockerfile", "frontend/Dockerfile"} <= dockerfiles
    yamls = {_rel(p) for p in _YAMLS}
    assert {
        "deploy/docker-compose.yml",
        "deploy/docker-compose.sso-dev.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/security.yml",
    } <= yamls
    scripts = {_rel(p) for p in _SCRIPTS}
    assert {"Makefile", "scripts/loadtest.sh", "scripts/git-hooks/pre-commit"} <= scripts


_DOCKERFILES = _files("Dockerfile*")
_YAMLS = _files("*.yml", "*.yaml")
_SCRIPTS = [*_files("*.sh"), ROOT / "Makefile", *_files("pre-commit")]


@pytest.mark.parametrize("path", _DOCKERFILES, ids=_rel)
def test_every_dockerfile_reference_is_pinned_by_digest(path: Path) -> None:
    assert unpinned_dockerfile_refs(path.read_text()) == []


@pytest.mark.parametrize("path", _YAMLS, ids=_rel)
def test_every_yaml_image_is_pinned_by_digest_or_built_here(path: Path) -> None:
    assert unpinned_yaml_images(path.read_text()) == []


@pytest.mark.parametrize("path", _SCRIPTS, ids=_rel)
def test_every_script_image_is_pinned_by_digest(path: Path) -> None:
    assert unpinned_script_images(path.read_text()) == []


def test_the_backend_image_does_not_install_tools_from_an_index() -> None:
    text = (ROOT / "backend/Dockerfile").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "pip install" not in code and "pip3 install" not in code


def test_uv_in_the_image_is_the_version_ci_uses() -> None:
    dockerfile = (ROOT / "backend/Dockerfile").read_text()
    in_image = re.search(r"^FROM ghcr\.io/astral-sh/uv:([\d.]+)@sha256:", dockerfile, re.MULTILINE)
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    in_ci = re.search(r'^\s*UV_VERSION:\s*"([\d.]+)"', ci, re.MULTILINE)
    assert in_image and in_ci
    assert in_image.group(1) == in_ci.group(1)


def test_pnpm_is_pinned_with_a_hash() -> None:
    text = (ROOT / "frontend/package.json").read_text()
    assert re.search(r'"packageManager":\s*"pnpm@[\d.]+\+sha512\.[0-9a-f]{128}"', text)
