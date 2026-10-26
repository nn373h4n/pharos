#!/usr/bin/env bash
# Creates deploy/.env from deploy/.env.example with freshly generated passwords.
# Refuses to overwrite an existing file or to write through a symbolic link.
# Usage: make env   (or: scripts/init-env.sh [target-file])
set -euo pipefail
caller=$PWD
cd "$(dirname "$0")/.."

target=${1:-deploy/.env}
# An explicit relative path is relative to where the command was run, the default to the repository.
if [ $# -gt 0 ]; then
  case "$target" in /*) ;; *) target="$caller/$target" ;; esac
fi
if [ -e "$target" ] || [ -L "$target" ]; then
  echo "$target already exists; leaving it untouched." >&2
  exit 1
fi
command -v openssl >/dev/null || { echo "openssl is needed to generate passwords." >&2; exit 1; }

umask 077
# noclobber makes the redirection fail instead of following a link created after the check above.
set -o noclobber
: > "$target"
set +o noclobber
while IFS= read -r line; do
  case "$line" in
    *=change-me) printf '%s=%s\n' "${line%%=*}" "$(openssl rand -hex 24)" >> "$target" ;;
    *) printf '%s\n' "$line" >> "$target" ;;
  esac
done < deploy/.env.example

echo "Created $target (mode 600) with generated passwords. Review PHAROS_DOMAIN and the ports."
