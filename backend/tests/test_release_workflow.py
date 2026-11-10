"""The release workflow publishes only from a version tag, with the least permission (M5-10b).

The workflow can write packages, so what it may do is pinned here: nothing is writable at the top,
the one job that writes packages runs only for a `v*` tag, a pull request never logs in to a
registry, every action is pinned by commit, and no workflow expression is pasted into a shell.
There is no YAML parser in the backend's own dependencies, so the file is read by its indentation.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/release.yml"
SHA_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}(\s+#.*)?$")


def top_level(text: str, key: str) -> str:
    """The block under a top-level key (without the key line)."""
    match = re.search(rf"^{re.escape(key)}:[^\n]*\n((?:(?:[ \t]+.*)?\n)*)", text, re.MULTILINE)
    return match.group(1) if match else ""


def jobs(text: str) -> dict[str, str]:
    """Job name -> its block, from the two-space-indented keys under `jobs:`."""
    body = top_level(text, "jobs")
    parts = re.split(r"^  (?=[A-Za-z0-9_-]+:\s*$)", body, flags=re.MULTILINE)
    result: dict[str, str] = {}
    for part in parts:
        head, _, rest = part.partition("\n")
        if head.endswith(":"):
            result[head[:-1]] = rest
    return result


def permissions(block: str, indent: int) -> dict[str, str]:
    """`permissions:` entries of a block whose keys sit at the given indent."""
    pad = " " * indent
    match = re.search(rf"^{pad}permissions:[ \t]*(\S.*)?\n((?:{pad}  .*\n)*)", block, re.MULTILINE)
    if not match:
        return {}
    if match.group(1):  # `permissions: write-all` or `permissions: {}` on one line
        return {"*": match.group(1).strip()}
    return dict(re.findall(r"^\s+([\w-]+):\s*(\S+)", match.group(2), re.MULTILINE))


def run_scripts(text: str) -> list[str]:
    """Every line that belongs to a `run:` step body."""
    lines = text.splitlines()
    found: list[str] = []
    i = 0
    while i < len(lines):
        match = re.match(r"^(\s*)(?:- )?run:\s*(?:&[\w-]+\s+)?(.*)$", lines[i])
        if not match:
            i += 1
            continue
        indent, rest = len(match.group(1)), match.group(2)
        if rest and not re.fullmatch(r"[|>][+\-0-9]*", rest):
            found.append(rest)
            i += 1
            continue
        i += 1
        while i < len(lines) and (
            not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent
        ):
            found.append(lines[i])
            i += 1
    return found


def violations(text: str) -> list[str]:
    """Everything wrong with a release workflow, as short names (empty when it is right)."""
    bad: list[str] = []
    top = permissions(text, 0)
    if top != {"contents": "read"}:
        bad.append("top-level permissions are not exactly contents: read")
    blocks = jobs(text)
    for name, block in blocks.items():
        granted = permissions(block, 4)
        writes = {k for k, v in granted.items() if v != "read" or k == "*"}
        allowed = {"packages"} if name == "publish" else set()
        if writes - allowed:
            bad.append(f"job {name} has extra permissions: {sorted(writes - allowed)}")
    publish = blocks.get("publish", "")
    if permissions(publish, 4).get("packages") != "write":
        bad.append("publish does not hold packages: write")
    if not re.search(
        r"^    if: startsWith\(github\.ref, 'refs/tags/v'\)\s*$", publish, re.MULTILINE
    ):
        bad.append("publish is not limited to a v* tag")
    if not re.search(r"^    needs: build-scan\s*$", publish, re.MULTILINE):
        bad.append("publish does not wait for build-scan")
    for name, block in blocks.items():
        if name != "publish" and re.search(
            r"docker (login|push)|ghcr\.io", "\n".join(run_scripts(block))
        ):
            bad.append(f"job {name} logs in or pushes")
    publish_code = "\n".join(run_scripts(publish))
    if re.search(r"docker\s+(image\s+)?build|buildx", publish_code):
        bad.append("publish rebuilds instead of pushing what was scanned")
    if 'gh run download "$RUN_ID"' not in publish_code:
        bad.append("publish does not take the images of this run")
    if "sha256sum -c" not in publish_code:
        bad.append("publish does not check the hashes of the scanned images")
    manifest, push = publish_code.find("docker manifest inspect"), publish_code.find("docker push")
    if manifest == -1 or push == -1 or manifest > push:
        bad.append("publish does not refuse to overwrite an existing tag before it pushes")
    if "GITHUB_OUTPUT" not in publish_code:
        bad.append("publish does not hand its digests on")
    if not re.search(r"- name: Log out\n\s+if: always\(\)\n\s+run: docker logout", publish):
        bad.append("publish does not always log out")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    if re.search(r"\bid-token\b|\bcontents:\s*write\b|write-all", code):
        bad.append("an id-token or contents: write permission appeared")
    triggers = set(re.findall(r"^  ([a-z_]+):", top_level(text, "on"), re.MULTILINE))
    if triggers != {"push", "pull_request"}:
        bad.append(
            f"a trigger other than a tag and a pull request on this file: {sorted(triggers)}"
        )
    if text.count("actions/checkout@") != text.count("persist-credentials: false"):
        bad.append("a checkout keeps the token in the repository")
    if re.search(
        r"^\s*(?:- )?uses:[ \t]+(?!actions/(checkout|upload-artifact)@)", text, re.MULTILINE
    ):
        bad.append("an action outside the allowed list")
    if (
        "if: startsWith(github.ref, 'refs/tags/v')\n        with:\n          name: images"
        not in text
    ):
        bad.append("the scanned images are uploaded on a pull request")
    for use in re.findall(r"^\s*(?:- )?uses:\s*(\S.*)$", text, re.MULTILINE):
        if not SHA_PIN.match(use.strip()):
            bad.append(f"action not pinned by commit: {use.strip()}")
    if any("${{" in line for line in run_scripts(text)):
        bad.append("a workflow expression is pasted into a shell script")
    scan = "\n".join(run_scripts(blocks.get("build-scan", "")))
    if not re.search(r"--severity HIGH,CRITICAL.*--exit-code 1", scan):
        bad.append("the scan does not fail the build on HIGH or CRITICAL")
    if "--format cyclonedx" not in scan:
        bad.append("no SBOM is made")
    if 'if ! git merge-base --is-ancestor "$GITHUB_SHA" FETCH_HEAD; then' not in scan:
        bad.append("the tagged commit is not checked against main")
    if "jq -e" not in scan or "fastapi" not in scan or "next" not in scan:
        bad.append("the SBOMs are not checked for the packages we ship")
    return bad


def test_the_release_workflow_is_as_designed() -> None:
    assert violations(WORKFLOW.read_text()) == []


def test_the_triggers_are_a_version_tag_and_a_pull_request_on_the_file() -> None:
    on = top_level(WORKFLOW.read_text(), "on")
    assert re.search(r'tags:\s*\["v\*"\]', on)
    assert re.search(r'paths:\s*\["\.github/workflows/release\.yml"\]', on)
    assert "branches" not in on  # a push to a branch must never publish


def test_the_images_are_labelled_with_their_source_and_revision() -> None:
    text = WORKFLOW.read_text()
    for label in ("source", "revision", "version", "licenses"):
        assert f"org.opencontainers.image.{label}=" in text


def test_the_scanned_images_and_the_sboms_leave_the_build_job() -> None:
    text = WORKFLOW.read_text()
    assert "name: sbom" in text and "name: images" in text
    assert "--format cyclonedx" in text


# The checker itself must bite: take the right workflow and break it one way at a time.
GOOD = WORKFLOW.read_text()


def _break(old: str, new: str) -> str:
    assert old in GOOD, old
    return GOOD.replace(old, new, 1)


BREAKS = {
    "top-level write": _break(
        "permissions:\n  contents: read\n\nconcurrency",
        "permissions:\n  contents: read\n  packages: write\n\nconcurrency",
    ),
    "top-level write-all": _break(
        "permissions:\n  contents: read\n\nconcurrency", "permissions: write-all\n\nconcurrency"
    ),
    "no tag guard": _break(
        "    if: startsWith(github.ref, 'refs/tags/v')\n    needs: build-scan",
        "    needs: build-scan",
    ),
    "guard on the wrong ref": _break(
        "    if: startsWith(github.ref, 'refs/tags/v')\n    needs: build-scan",
        "    if: github.event_name == 'push'\n    needs: build-scan",
    ),
    "login in the dry run": _break(
        "      - name: Scan the images\n        run: |\n          set -eu\n",
        "      - name: Scan the images\n        run: |\n          set -eu\n"
        "          echo x | docker login ghcr.io -u me --password-stdin\n",
    ),
    "id-token added": _break(
        "      actions: read\n      packages: write\n",
        "      actions: read\n      packages: write\n      id-token: write\n",
    ),
    "contents write on publish": _break(
        "      contents: read\n      actions: read\n",
        "      contents: write\n      actions: read\n",
    ),
    "pull_request_target": _break(
        "  pull_request:\n    paths", "  pull_request_target:\n    paths"
    ),
    "unpinned action": _break(
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1", "actions/checkout@v7"
    ),
    "expression in a script": _break(
        "          set -eu\n          labels=",
        '          set -eu\n          echo "${{ github.ref_name }}"\n          labels=',
    ),
    "scan does not fail": _break(
        "--ignore-unfixed --exit-code 1", "--ignore-unfixed --exit-code 0"
    ),
    "no sbom": _break("--format cyclonedx", "--format table"),
    "no main check": _break("merge-base --is-ancestor", "merge-base --is-descendant"),
    "publish rebuilds": _break(
        "            docker load -i",
        "            docker build -f backend/Dockerfile -t x .\n            docker load -i",
    ),
    "expression under an anchored block": _break(
        "        run: |\n          set -eu\n          if ! printf '%s' \"$TAG\"",
        "        run: &b |2\n          set -eu\n"
        "          echo ${{ github.event.head_commit.message }}\n"
        "          if ! printf '%s' \"$TAG\"",
    ),
    "publish rebuilds with docker image build": _break(
        "            docker load -i",
        "            docker image build -t x .\n            docker load -i",
    ),
    "workflow_run trigger": _break(
        "on:\n  push:", "on:\n  workflow_run:\n    workflows: [x]\n  push:"
    ),
    "main check ignored": _break(
        'if ! git merge-base --is-ancestor "$GITHUB_SHA" FETCH_HEAD; then',
        'git merge-base --is-ancestor "$GITHUB_SHA" FETCH_HEAD || true; if false; then',
    ),
    "no hash check": _break("| sha256sum -c -", "| cat"),
    "overwrite allowed": _break(
        'if docker manifest inspect "$target:$TAG" > /dev/null 2>&1; then', "if false; then"
    ),
    "no logout": _break(
        "        if: always()\n        run: docker logout ghcr.io",
        "        run: docker logout ghcr.io",
    ),
    "checkout keeps credentials": _break("persist-credentials: false", "persist-credentials: true"),
    "extra action": _break(
        "      - name: Check the tag",
        "      - uses: docker/login-action@" + "a" * 40 + " # v3\n      - name: Check the tag",
    ),
    "images uploaded on a pull request": _break(
        "        if: startsWith(github.ref, 'refs/tags/v')\n        with:\n          name: images",
        "        with:\n          name: images",
    ),
    "images of another run": _break('gh run download "$RUN_ID"', "gh run download"),
    "sbom not checked": _break("jq -e --argjson", "jq --argjson"),
    "digests not handed on": _break('echo "$image=$digest" >> "$GITHUB_OUTPUT"', "true"),
}


EXPECTED = {
    "top-level write": "top-level permissions",
    "top-level write-all": "top-level permissions",
    "no tag guard": "publish is not limited",
    "guard on the wrong ref": "publish is not limited",
    "login in the dry run": "job build-scan logs in or pushes",
    "id-token added": "extra permissions: ['id-token']",
    "contents write on publish": "extra permissions: ['contents']",
    "pull_request_target": "a trigger other than",
    "unpinned action": "action not pinned by commit",
    "expression in a script": "pasted into a shell script",
    "scan does not fail": "does not fail the build",
    "no sbom": "no SBOM",
    "no main check": "not checked against main",
    "publish rebuilds": "rebuilds instead of pushing",
    "expression under an anchored block": "pasted into a shell script",
    "publish rebuilds with docker image build": "rebuilds instead of pushing",
    "workflow_run trigger": "a trigger other than",
    "main check ignored": "not checked against main",
    "no hash check": "check the hashes",
    "overwrite allowed": "refuse to overwrite",
    "no logout": "always log out",
    "checkout keeps credentials": "keeps the token",
    "extra action": "outside the allowed list",
    "images uploaded on a pull request": "uploaded on a pull request",
    "images of another run": "images of this run",
    "sbom not checked": "SBOMs are not checked",
    "digests not handed on": "hand its digests",
}


def test_every_break_has_an_expected_message() -> None:
    assert set(EXPECTED) == set(BREAKS)


@pytest.mark.parametrize("name", sorted(BREAKS))
def test_every_way_of_breaking_the_workflow_is_noticed(name: str) -> None:
    found = violations(BREAKS[name])
    assert any(EXPECTED[name] in message for message in found), (name, found)


@pytest.mark.parametrize(
    "header", ["run: |", "run: |+", "run: |-", "run: |2", "run: >", "run: &a |", "- run: |"]
)
def test_run_scripts_reads_every_block_scalar_header(header: str) -> None:
    text = f"steps:\n  {header}\n    echo hidden\n  other: x\n"
    assert run_scripts(text) == ["    echo hidden"]
