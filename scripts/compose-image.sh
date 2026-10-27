#!/usr/bin/env bash
# Prints the image reference of a service from deploy/docker-compose.yml, so that the test scripts
# and the workflows use the same version as the stack instead of a copy that drifts when Dependabot
# updates the Compose file. Usage: scripts/compose-image.sh <service>   (for example: redis)
set -euo pipefail
cd "$(dirname "$0")/.."

[ $# -eq 1 ] || { echo "usage: $0 <service>" >&2; exit 2; }
image=$(awk -v service="$1" '
  $0 == "  " service ":" { inside = 1; next }
  inside && /^  [a-z]/ { inside = 0 }
  inside && /^    image: / { print $2; exit }
' deploy/docker-compose.yml)

if [ -z "$image" ]; then
  echo "no image found for service '$1'" >&2
  exit 1
fi
echo "$image"
