#!/usr/bin/env bash
# Verifies a published Pharos release: the signature and the SBOM attestation of both images.
# Usage: scripts/verify-release.sh <owner>/<repo> vX.Y.Z
#   for example: scripts/verify-release.sh my-org/pharos v0.1.0
#
# An image is accepted only if it was signed (keylessly, with Sigstore) by the release workflow of
# THIS repository, for THIS tag: the certificate must name
#   https://github.com/<owner>/<repo>/.github/workflows/release.yml@refs/tags/<tag>
# and have been issued to GitHub Actions. The script prints the digest of each image; pull and run
# by that digest, never by the tag (ADR-0024).
#
# It needs Docker and network access to ghcr.io and Sigstore. cosign runs as a pinned container.
# If the packages are private, log in first (`docker login ghcr.io`); DOCKER_CONFIG is passed on.
# The workflow uses the same script right after signing (ADR-0024, M5-10c).
set -euo pipefail

# cosign v3.1.4, pinned by digest (checked 2026-11-09). Dependabot does not see this line; move it
# with a deliberate change, together with COSIGN_IMAGE in .github/workflows/release.yml.
COSIGN_IMAGE=${COSIGN_IMAGE:-ghcr.io/sigstore/cosign/cosign:v3.1.4@sha256:0f06154836d2de34f887a5c273506d52ca43edaa1d9e53e6510fa78a48f69c2d}
ISSUER=https://token.actions.githubusercontent.com

usage() {
  echo "usage: $0 <owner>/<repo> vX.Y.Z" >&2
  exit 2
}

[ "$#" -eq 2 ] || usage
repo=$1
tag=$2
# `[[ =~ ]]` and not grep: grep matches line by line, so "x<newline>v0.1.0" would pass.
[[ $repo =~ ^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$ ]] || { echo "not owner/repo" >&2; usage; }
[[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || { echo "not a version tag" >&2; usage; }

owner=$(printf '%s' "${repo%%/*}" | tr '[:upper:]' '[:lower:]')
# What the certificate must say. PHAROS_VERIFY_IDENTITY_REF exists only so that the release
# workflow can prove a wrong identity is refused; an operator never sets it.
identity="https://github.com/${repo}/.github/workflows/release.yml@refs/tags/${PHAROS_VERIFY_IDENTITY_REF:-$tag}"

docker_args=(--rm)
if [ -n "${DOCKER_CONFIG:-}" ]; then
  docker_args+=(-v "$DOCKER_CONFIG:/dockercfg:ro" -e DOCKER_CONFIG=/dockercfg)
fi
cosign() { docker run "${docker_args[@]}" "$COSIGN_IMAGE" "$@"; }

for name in backend web; do
  package="ghcr.io/${owner}/pharos-${name}"
  digest=$(docker buildx imagetools inspect "${package}:${tag}" --format '{{.Manifest.Digest}}')
  [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "no digest for ${package}:${tag}" >&2; exit 1; }
  ref="${package}@${digest}"
  echo "== ${name}: ${ref}"
  cosign verify --certificate-identity "$identity" --certificate-oidc-issuer "$ISSUER" "$ref" > /dev/null
  cosign verify-attestation --type cyclonedx --certificate-identity "$identity" \
    --certificate-oidc-issuer "$ISSUER" "$ref" > /dev/null
  echo "OK: signed by ${identity}, SBOM attached"
done
echo "Pull by these digests, not by the tag."
