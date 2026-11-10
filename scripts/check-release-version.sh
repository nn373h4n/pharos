#!/usr/bin/env bash
# Checks that a release tag names the version the code says it is (M5-10d, ADR-0024).
# Usage: scripts/check-release-version.sh vX.Y.Z[-suffix] [<repo-root>]
#
# The core X.Y.Z of the tag must equal the version in backend/pyproject.toml and in
# frontend/package.json. A tag cut before the version was raised ("release prep") fails here,
# before anything is built or published. The suffix of a pre-release tag (-rc.1) is not compared.
set -euo pipefail

[ "$#" -eq 1 ] || [ "$#" -eq 2 ] || { echo "usage: $0 vX.Y.Z [<repo-root>]" >&2; exit 2; }
tag=$1
root=${2:-$(cd "$(dirname "$0")/.." && pwd)}

# `[[ =~ ]]` and not grep: grep matches line by line, so "x<newline>v0.1.0" would pass.
[[ $tag =~ ^v([0-9]+\.[0-9]+\.[0-9]+)(-[0-9A-Za-z.]+)?$ ]] || { echo "not a version tag" >&2; exit 2; }
core=${BASH_REMATCH[1]}

backend=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$root/backend/pyproject.toml" | head -n 1)
frontend=$(sed -n 's/^  "version": "\(.*\)",$/\1/p' "$root/frontend/package.json" | head -n 1)

status=0
if [ "$backend" != "$core" ]; then
  echo "backend/pyproject.toml says '${backend:-nothing}', the tag says $core" >&2
  status=1
fi
if [ "$frontend" != "$core" ]; then
  echo "frontend/package.json says '${frontend:-nothing}', the tag says $core" >&2
  status=1
fi
if [ "$status" -ne 0 ]; then
  echo "Raise the version in both files first (docs/guides/releasing.md)." >&2
  exit 1
fi
echo "Version $core matches the tag $tag."
