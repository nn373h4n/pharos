"""scripts/ollama-pull.sh hands an operator's model name to Docker, so it must refuse anything that
is not a model name before Docker is ever started (M4-10, ADR-0020). Docker is replaced by a
script that records how it was called."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ollama-pull.sh"


@pytest.fixture
def fake_docker(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\necho "$PHAROS_OLLAMA_PULL_MODEL | $*" >> {log}\n')
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def run(fake: Path, *args: str) -> tuple[int, str]:
    env = {"PATH": f"{fake / 'bin'}:/usr/bin:/bin", "HOME": str(fake)}
    done = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT), *args],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    log = fake / "docker.log"
    return done.returncode, log.read_text() if log.exists() else ""


@pytest.mark.parametrize(
    "name",
    [
        "a;b",
        "--help",
        "-x",
        "a b",
        "$(id)",
        "`id`",
        "a\nb",
        "../x",
        "x" * 101,
        "",
        ":tag",
        "a|b",
        "a&b",
        "evil.example.com/x/y:1",  # another registry
        "hf.co/user/model",
        "localhost:5000/x",
        "a/../../b",
        "a..b",
    ],
)
def test_a_name_that_is_not_a_model_name_never_reaches_docker(fake_docker: Path, name: str) -> None:
    code, log = run(fake_docker, name)
    assert code == 2 and log == ""


def test_the_wrong_number_of_arguments_is_refused(fake_docker: Path) -> None:
    assert run(fake_docker) == (2, "")
    assert run(fake_docker, "a", "b") == (2, "")


@pytest.mark.parametrize(
    "name", ["qwen2.5:7b", "library/llama3.1:8b-instruct-q4_K_M", "bge-m3", "someone/model.v2:1.5"]
)
def test_a_model_name_runs_only_the_download_job_with_the_name_in_the_environment(
    fake_docker: Path, name: str
) -> None:
    code, log = run(fake_docker, name)
    assert code == 0
    [line] = log.strip().splitlines()
    assert line.startswith(f"{name} | compose -f deploy/docker-compose.yml")
    assert line.endswith("--profile local-llm-pull run --rm ollama-pull")
    assert os.path.exists(SCRIPT)
