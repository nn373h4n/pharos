"""The pre-commit secret scan blocks a commit unless gitleaks really ran and found nothing.

It once passed every commit made in a linked worktree: gitleaks could not see the main repository's
`.git` inside its container, printed an error, said "no leaks found" about nothing and exited 0.
"""

import stat
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "scripts" / "git-hooks" / "pre-commit"

FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" > "$FAKE_DIR/args"
printf '%b' "$FAKE_OUTPUT"
exit "$FAKE_EXIT"
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def run_hook(
    cwd: Path, tmp_path: Path, output: str, code: int = 0
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FAKE_DIR": str(tmp_path),
        "FAKE_OUTPUT": output,
        "FAKE_EXIT": str(code),
    }
    return subprocess.run(  # noqa: S603
        ["bash", str(HOOK)],  # noqa: S607
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(
        main,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.test",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "first",
    )
    return main


def test_a_clean_scan_lets_the_commit_through(repo: Path, tmp_path: Path) -> None:
    done = run_hook(repo, tmp_path, "INF no leaks found\\n")
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    "output",
    [
        "ERR [git] fatal: not a git repository: /x/.git/worktrees/y\\nINF no leaks found\\n",
        "",  # nothing printed: nobody knows what was scanned
    ],
)
def test_a_scan_that_did_not_run_cleanly_blocks_the_commit(
    repo: Path, tmp_path: Path, output: str
) -> None:
    done = run_hook(repo, tmp_path, output, code=0)
    assert done.returncode == 1
    assert "commit blocked" in done.stderr


def test_a_leak_blocks_the_commit(repo: Path, tmp_path: Path) -> None:
    done = run_hook(repo, tmp_path, "WRN leaks found: 1\\n", code=1)
    assert done.returncode == 1


def test_in_a_linked_worktree_the_main_repository_is_mounted_too(
    repo: Path, tmp_path: Path
) -> None:
    worktree = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", str(worktree))
    done = run_hook(worktree, tmp_path, "INF no leaks found\\n")
    assert done.returncode == 0, done.stderr
    args = (tmp_path / "args").read_text()
    common = git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    assert f"{worktree.resolve()}:{worktree.resolve()}:ro" in args
    assert f"{common}:{common}:ro" in args
    assert args.rstrip().endswith(str(worktree.resolve()))


def test_in_the_main_checkout_only_the_repository_is_mounted(repo: Path, tmp_path: Path) -> None:
    done = run_hook(repo, tmp_path, "INF no leaks found\\n")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "args").read_text().count(" -v ") == 1
