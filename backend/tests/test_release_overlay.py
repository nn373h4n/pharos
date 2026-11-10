"""The release overlay swaps every image Pharos builds for a published one (M5-10d, ADR-0024).

If a service is added to the base file that uses `pharos-backend` or `pharos-web` and the overlay is
not told, that service would still be built on the operator's host while the rest run the release.
This test fails for that, for a service that keeps its build section, and for a reference that
Compose would not insist on.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = (ROOT / "deploy/docker-compose.yml").read_text()
OVERLAY = (ROOT / "deploy/docker-compose.release.yml").read_text()

LOCAL = re.compile(r"image:\s*pharos-(backend|web):\$\{PHAROS_IMAGE_TAG:-local\}")


def services_by_block(text: str) -> dict[str, str]:
    body = text.split("\nservices:\n", 1)[1]
    parts = re.split(r"^  (?=[a-z0-9-]+:\s*$)", body, flags=re.MULTILINE)
    out: dict[str, str] = {}
    for part in parts:
        head, _, rest = part.partition("\n")
        if head.endswith(":"):
            out[head[:-1]] = rest
    return out


def built_here(text: str) -> dict[str, str]:
    """Service -> `backend` or `web`, for each service whose image is built from this repository."""
    found = {}
    for name, block in services_by_block(text).items():
        match = LOCAL.search(block)
        if match:
            found[name] = match.group(1)
        elif re.search(r"^    build:", block, re.MULTILINE):
            found[name] = "?"  # built here, but not under a name the overlay knows
    return found


def test_the_overlay_names_exactly_the_services_that_are_built_here() -> None:
    assert built_here(BASE)  # the scan sees services at all
    assert set(services_by_block(OVERLAY)) == set(built_here(BASE))


def test_every_service_that_is_built_here_has_a_known_image() -> None:
    assert "?" not in built_here(BASE).values()


def test_each_service_gets_the_right_image_and_loses_its_build_section() -> None:
    wanted = {"backend": "PHAROS_BACKEND_IMAGE", "web": "PHAROS_WEB_IMAGE"}
    for name, block in services_by_block(OVERLAY).items():
        variable = wanted[built_here(BASE)[name]]
        assert re.search(rf"^    image: \$\{{{variable}:\?[^}}]+\}}$", block, re.MULTILINE), name
        assert re.search(r"^    build: !reset null$", block, re.MULTILINE), name
        assert "pharos-backend" not in block and "pharos-web" not in block, name


def test_the_overlay_does_not_touch_anything_but_images_and_builds() -> None:
    for name, block in services_by_block(OVERLAY).items():
        keys = set(re.findall(r"^    ([a-z_]+):", block, re.MULTILINE))
        assert keys == {"build", "image"}, (name, keys)
    assert "\nnetworks:" not in OVERLAY and "\nvolumes:" not in OVERLAY
    assert "ports:" not in OVERLAY and "environment:" not in OVERLAY


def test_the_overlay_is_documented_for_operators() -> None:
    text = (ROOT / "docs/DEPLOYMENT.md").read_text()
    assert "docker-compose.release.yml" in text
    assert "PHAROS_BACKEND_IMAGE" in text and "PHAROS_WEB_IMAGE" in text
