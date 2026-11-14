"""`scripts/verify-release.sh` accepts a release only for the right identity (M5-10c, ADR-0024).

Docker is replaced by a stub that records every call, so the arguments of the real cosign run are
visible without a network. What is proven: bad input is refused before any call; the identity named
to cosign is the release workflow of the stated repository for the stated tag; both images are
checked by digest, for the signature and for the SBOM attestation; and a failing cosign, a missing
digest or a malformed digest fails the script. Real signatures: NOT tested here (the release
workflow proves them in the pipeline).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-release.sh"
DIGEST = "sha256:" + "ab" * 32

STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
if [ "$1" = buildx ]; then
  printf '%s' "${STUB_DIGEST-}"
  exit "${STUB_BUILDX_EXIT:-0}"
fi
if [ "$1" = run ]; then
  case " $* " in
    *" ${STUB_FAIL_ON:-__never__} "*) exit 1 ;;
  esac
  exit 0
fi
exit 0
"""


def run(
    tmp_path: Path, *args: str, env: dict[str, str] | None = None, checkout: Path | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Runs a copy of the script in `checkout/scripts/` (by default a folder that is no git
    checkout, so the commit of the checkout is not compared unless a test makes one)."""
    root = checkout or tmp_path / "release"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    script = root / "scripts" / SCRIPT.name
    shutil.copy2(SCRIPT, script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(STUB)
    docker.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    full_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_DIGEST": DIGEST,
        "HOME": str(tmp_path),
        **(env or {}),
    }
    result = subprocess.run(  # noqa: S603
        [str(script), *args], env=full_env, capture_output=True, text=True, timeout=60
    )
    return result, log.read_text().splitlines()


ID = "https://github.com/Acme-Org/pharos/.github/workflows/release.yml@refs/tags/v0.1.0"
ISSUER = "https://token.actions.githubusercontent.com"


def test_a_good_release_is_verified_by_digest_for_both_images(tmp_path: Path) -> None:
    result, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0")
    assert result.returncode == 0, result.stderr
    runs = [c for c in calls if c.startswith("run ")]
    assert len(runs) == 4  # verify and verify-attestation, for two images
    for name in ("backend", "web"):
        ref = f"ghcr.io/acme-org/pharos-{name}@{DIGEST}"  # the owner is lower-cased for GHCR
        mine = [c for c in runs if c.endswith(ref)]
        assert len(mine) == 2
        assert any(" verify " in c and "verify-attestation" not in c for c in mine)
        assert any("verify-attestation --type cyclonedx" in c for c in mine)
    for call in runs:
        assert f"--certificate-identity {ID}" in call
        assert f"--certificate-oidc-issuer {ISSUER}" in call
        assert "@sha256:0f06154836d2de34f887a5c273506d52ca43edaa1d9e53e6510fa78a48f69c2d" in call
        assert "--insecure-ignore-tlog" not in call and "--key" not in call
    assert "Pull by these digests" in result.stdout


def test_environment_variables_cannot_replace_the_verifier_or_weaken_the_identity(
    tmp_path: Path,
) -> None:
    result, calls = run(
        tmp_path,
        "Acme-Org/pharos",
        "v0.1.0",
        env={"COSIGN_IMAGE": "evil/always-ok:latest", "PHAROS_VERIFY_IDENTITY_REF": "v9.9.9"},
    )
    assert result.returncode == 0
    runs = [c for c in calls if c.startswith("run ")]
    assert len(runs) == 4
    assert not any("evil" in c for c in runs)
    assert all(ID in c and "v9.9.9" not in c for c in runs)


def test_the_repository_and_the_trigger_are_always_pinned(tmp_path: Path) -> None:
    _, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0")
    runs = [c for c in calls if c.startswith("run ")]
    assert all("--certificate-github-workflow-repository Acme-Org/pharos" in c for c in runs)
    assert all("--certificate-github-workflow-trigger push" in c for c in runs)
    assert not any("--certificate-github-workflow-sha" in c for c in runs)


def test_without_a_commit_the_script_says_the_commit_is_not_checked(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "Acme-Org/pharos", "v0.1.0")
    assert result.returncode == 0
    assert "commit that was built is NOT checked" in result.stderr


def test_a_given_commit_is_pinned_in_the_certificate_check(tmp_path: Path) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    result, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0", sha)
    assert result.returncode == 0
    runs = [c for c in calls if c.startswith("run ")]
    assert len(runs) == 4
    assert all(f"--certificate-github-workflow-sha {sha}" in c for c in runs)
    assert "NOT checked" not in result.stderr


def test_docker_config_is_passed_on_read_only_when_set(tmp_path: Path) -> None:
    _, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0", env={"DOCKER_CONFIG": "/some/cfg"})
    runs = [c for c in calls if c.startswith("run ")]
    assert all("-v /some/cfg:/dockercfg:ro -e DOCKER_CONFIG=/dockercfg" in c for c in runs)
    _, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0")
    assert not any("dockercfg" in c for c in calls)


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("Acme-Org/pharos",),
        ("Acme-Org", "v0.1.0"),
        ("Acme-Org/pharos", "0.1.0"),
        ("Acme-Org/pharos", "latest"),
        ("Acme-Org/pharos", "v0.1"),
        ("Acme-Org/pharos", "v0.1.0; rm -x"),
        ("Acme-Org/pharos", "v0.1.0\n"),
        ("Acme-Org/pharos", "x\nv0.1.0"),
        ("Acme-Org/pharos\nx/y", "v0.1.0"),
        ("Acme Org/pharos", "v0.1.0"),
        ("../pharos", "v0.1.0"),
        ("Acme-Org/pharos/x", "v0.1.0"),
        ("Acme-Org/pharos", "v0.1.0", "abc123"),
        ("Acme-Org/pharos", "v0.1.0", "0123456789ABCDEF0123456789ABCDEF01234567"),
        ("Acme-Org/pharos", "v0.1.0", "0123456789abcdef0123456789abcdef01234567\nx"),
        ("Acme-Org/pharos", "v0.1.0", "0123456789abcdef0123456789abcdef01234567", "extra"),
    ],
)
def test_bad_input_is_refused_before_any_docker_call(tmp_path: Path, args: tuple[str, ...]) -> None:
    result, calls = run(tmp_path, *args)
    assert result.returncode == 2
    assert calls == []


def test_a_prerelease_tag_is_accepted(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "Acme-Org/pharos", "v0.1.0-rc.1")
    assert result.returncode == 0


@pytest.mark.parametrize("fail_on", ["verify", "verify-attestation"])
def test_a_failing_cosign_fails_the_script(tmp_path: Path, fail_on: str) -> None:
    result, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0", env={"STUB_FAIL_ON": fail_on})
    assert result.returncode != 0
    assert "OK: signed by" not in result.stdout
    assert "Pull by these digests" not in result.stdout
    assert any(c.startswith("run ") for c in calls)  # it did reach cosign


@pytest.mark.parametrize(
    "digest", ["", "latest", "sha256:abc", "sha256:" + "AB" * 32, DIGEST + "x", "junk\n" + DIGEST]
)
def test_a_missing_or_malformed_digest_stops_before_cosign(tmp_path: Path, digest: str) -> None:
    result, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0", env={"STUB_DIGEST": digest})
    assert result.returncode != 0
    assert not any(c.startswith("run ") for c in calls)


def test_a_registry_error_stops_the_script(tmp_path: Path) -> None:
    result, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0", env={"STUB_BUILDX_EXIT": "1"})
    assert result.returncode != 0
    assert not any(c.startswith("run ") for c in calls)


BACKEND = f"ghcr.io/acme-org/pharos-backend@{DIGEST}"
WEB = f"ghcr.io/acme-org/pharos-web@{DIGEST}"


def test_references_that_match_the_release_pass_and_no_note_is_printed(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "Acme-Org/pharos", "v0.1.0", "--backend", BACKEND, "--web", WEB)
    assert result.returncode == 0, result.stderr
    assert "NOT compared" not in result.stderr


def test_without_references_the_script_says_none_were_compared(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "Acme-Org/pharos", "v0.1.0")
    assert result.returncode == 0
    assert "NOT compared" in result.stderr


def test_one_reference_alone_still_says_the_other_was_not_compared(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "Acme-Org/pharos", "v0.1.0", "--backend", BACKEND)
    assert result.returncode == 0
    assert "NOT compared" in result.stderr


def test_flags_may_come_after_the_commit(tmp_path: Path) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    result, _ = run(tmp_path, "Acme-Org/pharos", "v0.1.0", sha, "--web", WEB, "--backend", BACKEND)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "flag, value",
    [
        ("--backend", f"ghcr.io/other-org/pharos-backend@{DIGEST}"),  # another owner
        ("--backend", f"ghcr.io/acme-org/pharos-backend@sha256:{'cd' * 32}"),  # another digest
        ("--web", f"ghcr.io/acme-org/pharos-backend@{DIGEST}"),  # the wrong image
        ("--web", f"ghcr.io/acme-org/pharos-web@sha256:{'ab' * 31}cd"),  # one byte off
    ],
)
def test_a_reference_that_differs_from_the_release_fails_before_cosign(
    tmp_path: Path, flag: str, value: str
) -> None:
    result, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0", flag, value)
    assert result.returncode == 1
    assert "FAIL" in result.stderr
    name = flag.removeprefix("--")  # the image whose reference was wrong is not verified at all
    assert not any(c.startswith("run ") and f"/pharos-{name}@" in c for c in calls)


@pytest.mark.parametrize(
    "value",
    [
        "ghcr.io/acme-org/pharos-backend:v0.1.0",  # a tag can be moved
        "pharos-backend:local",  # built on the host
        f"ghcr.io/Acme-Org/pharos-backend@{DIGEST}",  # not lower case
        f"docker.io/acme-org/pharos-backend@{DIGEST}",  # another registry
        f"ghcr.io/acme-org/pharos-backend@{DIGEST}\nx",
        "",
    ],
)
def test_a_reference_not_in_the_digest_form_is_refused_before_any_docker_call(
    tmp_path: Path, value: str
) -> None:
    result, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0", "--backend", value)
    if value == "":  # an empty value means "not given"
        assert result.returncode == 0
        return
    assert result.returncode == 2
    assert calls == []


def test_a_flag_without_a_value_is_refused(tmp_path: Path) -> None:
    result, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0", "--web")
    assert result.returncode == 2
    assert calls == []


# --- the checkout is the release's commit (M5-13 review) -----------------------------------------

GIT = shutil.which("git") or "git"


def git_checkout(path: Path, content: str = "release") -> str:
    """A checkout with one commit; returns its sha (the content makes the sha differ)."""
    path.mkdir(parents=True)
    git = [GIT, "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.test"]
    subprocess.run([*git[:3], "init", "-q"], check=True)  # noqa: S603
    (path / "README").write_text(f"{content}\n")
    subprocess.run([*git, "add", "README"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-q", "-m", "release"], check=True)  # noqa: S603
    return subprocess.run(  # noqa: S603
        [GIT, "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_a_checkout_at_the_release_commit_is_accepted(tmp_path: Path) -> None:
    sha = git_checkout(tmp_path / "pharos")
    result, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0", sha, checkout=tmp_path / "pharos")
    assert result.returncode == 0, result.stderr
    assert "OK: this checkout is the release's commit" in result.stdout
    assert len([c for c in calls if c.startswith("run ")]) == 4


def test_a_checkout_at_another_commit_is_refused_before_any_image_check(tmp_path: Path) -> None:
    git_checkout(tmp_path / "pharos")
    other = "0123456789abcdef0123456789abcdef01234567"
    result, calls = run(tmp_path, "Acme-Org/pharos", "v0.1.0", other, checkout=tmp_path / "pharos")
    assert result.returncode == 1
    assert "this checkout is at" in result.stderr and other in result.stderr
    assert calls == []  # nothing was asked of Docker


def test_git_variables_cannot_point_the_comparison_at_another_repository(tmp_path: Path) -> None:
    sha = git_checkout(tmp_path / "elsewhere", "another repository")
    git_checkout(tmp_path / "pharos")
    result, _ = run(
        tmp_path,
        "Acme-Org/pharos",
        "v0.1.0",
        sha,
        checkout=tmp_path / "pharos",
        env={"GIT_DIR": str(tmp_path / "elsewhere" / ".git")},
    )
    assert result.returncode == 1  # the checkout's own commit was read, not the other one


def test_outside_a_checkout_the_script_says_the_files_were_not_compared(tmp_path: Path) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    result, _ = run(tmp_path, "Acme-Org/pharos", "v0.1.0", sha)
    assert result.returncode == 0
    assert "Compose files were NOT compared" in result.stderr
