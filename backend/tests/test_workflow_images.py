"""The tool images the workflows and scripts run follow `.github/images/Dockerfile` (F-56 b).

Dependabot proposes new versions of what a Dockerfile names, not of a `*_IMAGE` variable in a
workflow. The file lists those images so Dependabot sees them; this test keeps every copy equal to
it, so a Dependabot bump fails here and names the places to change, and a change made in one place
alone cannot pass either.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TRACKER = ROOT / ".github/images/Dockerfile"
FROM = re.compile(
    r"^FROM (?P<name>[^:@\s]+):(?P<tag>[\w.-]+)@(?P<digest>sha256:[0-9a-f]{64}) AS (?P<stage>\w+)$"
)

# Where each image is written, as (file, pattern with the reference in the group `ref` and, for a
# digest-only reference, the version in a trailing comment in the group `tag`).
COPIES = {
    "trivy": [
        (".github/workflows/security.yml", r"^  TRIVY_IMAGE: (?P<ref>\S+) # (?P<tag>\S+)$"),
        (".github/workflows/release.yml", r"^  TRIVY_IMAGE: (?P<ref>\S+) # (?P<tag>\S+)$"),
    ],
    "cosign": [
        (".github/workflows/release.yml", r"^  COSIGN_IMAGE: (?P<ref>\S+) # (?P<tag>\S+)$"),
        ("scripts/verify-release.sh", r"^readonly COSIGN_IMAGE=(?P<ref>\S+)$"),
    ],
}


def tracked() -> dict[str, re.Match[str]]:
    images = {}
    for line in TRACKER.read_text().splitlines():
        if line.startswith("FROM "):
            match = FROM.fullmatch(line)
            assert match, f"not a pinned FROM line: {line}"
            images[match["stage"]] = match
    return images


def test_the_tracker_lists_every_tool_image_and_only_those() -> None:
    assert set(tracked()) == set(COPIES)


def test_dependabot_reads_the_tracker() -> None:
    config = (ROOT / ".github/dependabot.yml").read_text()
    assert re.search(r"^      - /\.github/images\b", config, re.MULTILINE)


@pytest.mark.parametrize(
    ("stage", "path", "pattern"),
    [(stage, path, pattern) for stage, copies in COPIES.items() for path, pattern in copies],
)
def test_every_copy_is_the_tracked_image(stage: str, path: str, pattern: str) -> None:
    image = tracked()[stage]
    matches = list(re.finditer(pattern, (ROOT / path).read_text(), re.MULTILINE))
    assert len(matches) == 1, f"{path}: expected one {stage} reference"
    match = matches[0]
    ref = match["ref"]
    hint = f"{path} names another {stage} image than .github/images/Dockerfile: copy it there"
    name, _, digest = ref.partition("@")
    assert digest == image["digest"], hint
    assert name in (image["name"], f"{image['name']}:{image['tag']}"), hint
    if match.groupdict().get("tag") is not None:
        assert match["tag"].lstrip("v") == image["tag"].lstrip("v"), hint + " (and its comment)"
