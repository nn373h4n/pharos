#!/usr/bin/env bash
# Fails when a commit in <base>..<head> has no "Signed-off-by:" line (the Developer Certificate of
# Origin, added by `git commit -s`). Merge commits and commits by GitHub bots such as Dependabot
# are skipped: a bot cannot certify anything, and the maintainer who merges its pull request takes
# that responsibility. Usage: scripts/check-dco.sh <base-ref> <head-ref>
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <base-ref> <head-ref>" >&2
  exit 2
fi
base=$1
head=$2

missing=0
checked=0
while read -r commit; do
  email=$(git show -s --format=%ae "$commit")
  case "$email" in
    *"[bot]@users.noreply.github.com") continue ;;
  esac
  checked=$((checked + 1))
  if ! git show -s --format=%B "$commit" | grep -qE '^Signed-off-by: .+ <[^<>@ ]+@[^<>@ ]+>$'; then
    echo "No Signed-off-by line: $(git show -s --format='%h %s' "$commit")"
    missing=$((missing + 1))
  fi
done < <(git rev-list --no-merges "$base..$head")

if [ "$missing" -gt 0 ]; then
  echo "$missing of $checked commit(s) are not signed off. Add the line with: git commit --amend -s (or git rebase --signoff)." >&2
  exit 1
fi
echo "All $checked commit(s) are signed off."
