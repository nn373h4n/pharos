#!/usr/bin/env bash
# Verifies a published Pharos release: the signature and the SBOM attestation of both images.
# Usage: scripts/verify-release.sh <owner>/<repo> vX.Y.Z [<commit-sha>]
#   for example: scripts/verify-release.sh my-org/pharos v0.1.0 0123456789abcdef0123456789abcdef01234567
#
# An image is accepted only if it was signed (keylessly, with Sigstore) by the release workflow of
# THIS repository, for THIS tag: the certificate must name
#   https://github.com/<owner>/<repo>/.github/workflows/release.yml@refs/tags/<tag>
# and have been issued to GitHub Actions for a push of that repository. With the optional commit
# the certificate must also name that commit: the release notes state it, and it is the only check
# that notices a tag that was deleted and recreated on another commit. Without it the commit is
# NOT checked. The script prints the digest of each image; pull and run by that digest, never by
# the tag (ADR-0024).
#
# It needs Docker and network access to ghcr.io and Sigstore. cosign runs as a pinned container.
# If the packages are private, log in first (`docker login ghcr.io`); DOCKER_CONFIG is passed on.
# The workflow uses the same script right after signing (ADR-0024, M5-10c).
set -euo pipefail

# cosign v3.1.4, pinned by digest (checked 2026-11-09). Dependabot does not see this line; move it
# with a deliberate change, together with COSIGN_IMAGE in .github/workflows/release.yml. It is
# fixed on purpose: a variable from the caller's environment must not be able to replace the verifier.
readonly COSIGN_IMAGE=ghcr.io/sigstore/cosign/cosign:v3.1.4@sha256:0f06154836d2de34f887a5c273506d52ca43edaa1d9e53e6510fa78a48f69c2d
ISSUER=https://token.actions.githubusercontent.com

usage() {
  echo "usage: $0 <owner>/<repo> vX.Y.Z [<commit-sha>]" >&2
  exit 2
}

[ "$#" -eq 2 ] || [ "$#" -eq 3 ] || usage
repo=$1
tag=$2
sha=${3:-}
# `[[ =~ ]]` and not grep: grep matches line by line, so "x<newline>v0.1.0" would pass.
[[ $repo =~ ^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$ ]] || { echo "not owner/repo" >&2; usage; }
[[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || { echo "not a version tag" >&2; usage; }
[[ -z $sha || $sha =~ ^[0-9a-f]{40}$ ]] || { echo "not a full commit sha" >&2; usage; }

owner=$(printf '%s' "${repo%%/*}" | tr '[:upper:]' '[:lower:]')
# What the certificate must say: this repository's release workflow, for this tag, started by a push
# (not a pull request or a manual run), and, when given, built from this commit.
identity="https://github.com/${repo}/.github/workflows/release.yml@refs/tags/${tag}"
pins=(--certificate-identity "$identity" --certificate-oidc-issuer "$ISSUER"
  --certificate-github-workflow-repository "$repo" --certificate-github-workflow-trigger push)
if [ -n "$sha" ]; then
  pins+=(--certificate-github-workflow-sha "$sha")
fi

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
  cosign verify "${pins[@]}" "$ref" > /dev/null
  cosign verify-attestation --type cyclonedx "${pins[@]}" "$ref" > /dev/null
  echo "OK: signed by ${identity}, SBOM attached"
done
if [ -z "$sha" ]; then
  echo "NOTE: no commit given, so the commit that was built is NOT checked." >&2
fi
echo "Pull by these digests, not by the tag."
