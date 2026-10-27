#!/usr/bin/env bash
# Checks scripts/check-dco.sh against a throwaway repository.
# Usage: bash tests/scripts/test_check_dco.sh
set -u
root="$(cd "$(dirname "$0")/../.." && pwd)"
work=$(mktemp -d)
trap 'rm -r "$work"' EXIT
failed=0

check() { # description, expected exit status, command...
  local description=$1 expected=$2
  shift 2
  "$@" >/dev/null 2>&1
  local actual=$?
  if [ "$actual" -eq "$expected" ]; then printf 'ok    %s\n' "$description"; else printf 'FAIL  %s (exit %s, expected %s)\n' "$description" "$actual" "$expected"; failed=$((failed + 1)); fi
}

cd "$work"
git init -q -b main
git config user.name "Test Person"
git config user.email "test@example.test"
git config commit.gpgsign false
echo base > file && git add file && git commit -q -s -m "base"
git switch -q -c feature

echo one > one && git add one && git commit -q -s -m "signed commit"
check "a signed-off commit passes" 0 bash "$root/scripts/check-dco.sh" main HEAD

echo two > two && git add two && git commit -q -m "unsigned commit"
check "an unsigned commit fails" 1 bash "$root/scripts/check-dco.sh" main HEAD

echo three > three && git add three && git commit -q -s -m "another signed commit"
check "one unsigned commit among signed ones still fails" 1 bash "$root/scripts/check-dco.sh" main HEAD
check "the range after the unsigned commit passes" 0 bash "$root/scripts/check-dco.sh" HEAD~1 HEAD

git switch -q main && git switch -q -c bot
echo bot > bot && git add bot
git -c user.name="dependabot[bot]" -c user.email="49699333+dependabot[bot]@users.noreply.github.com" commit -q -m "build: bump a dependency"
check "a commit by a GitHub bot is exempt" 0 bash "$root/scripts/check-dco.sh" main HEAD

git switch -q main && git switch -q -c fakebot
echo fake > fake && git add fake
git -c user.email="someone@example.test" commit -q -m "claims to be a bot [bot]"
check "a human commit with [bot] in the message is not exempt" 1 bash "$root/scripts/check-dco.sh" main HEAD

git switch -q main && git switch -q -c badtrailer
echo trailer > trailer && git add trailer
git commit -q -m "text mentioning Signed-off-by: nobody" -m "Signed-off-by: missing email"
check "a malformed Signed-off-by line does not count" 1 bash "$root/scripts/check-dco.sh" main HEAD

git switch -q main
git merge -q --no-ff --no-edit feature -m "Merge feature" 2>/dev/null
check "merge commits are skipped" 1 bash "$root/scripts/check-dco.sh" main~1 HEAD
check "an empty range passes" 0 bash "$root/scripts/check-dco.sh" HEAD HEAD
check "missing arguments are a usage error" 2 bash "$root/scripts/check-dco.sh"

if [ "$failed" -ne 0 ]; then printf '\n%s case(s) FAILED\n' "$failed"; exit 1; fi
printf '\nall cases passed\n'
