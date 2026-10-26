#!/usr/bin/env bash
# Checks scripts/init-env.sh: generated secrets, file mode, no overwrite.
# Usage: bash tests/scripts/test_init_env.sh
set -u
root="$(cd "$(dirname "$0")/../.." && pwd)"
work=$(mktemp -d)
trap 'rm -r "$work"' EXIT
target="$work/generated.env"
failed=0

check() { # description, command...
  local description=$1
  shift
  if "$@"; then printf 'ok    %s\n' "$description"; else printf 'FAIL  %s\n' "$description"; failed=$((failed + 1)); fi
}

bash "$root/scripts/init-env.sh" "$target" >/dev/null 2>&1
check "creates the file" test -f "$target"
check "file mode is 600" test "$(stat -c %a "$target")" = 600
secrets=$(grep -E '^(POSTGRES_PASSWORD|PHAROS_OWNER_PASSWORD|PHAROS_APP_PASSWORD|PHAROS_REDIS_PASSWORD)=' "$target")
check "four secrets are present" test "$(echo "$secrets" | wc -l)" = 4
check "no placeholder is left in the secrets" test "$(echo "$secrets" | grep -c 'change-me')" = 0
check "every secret is 48 hex characters" test "$(echo "$secrets" | grep -cE '=[0-9a-f]{48}$')" = 4
check "the four secrets are all different" test "$(echo "$secrets" | cut -d= -f2 | sort -u | wc -l)" = 4
check "non-secret settings are copied unchanged" grep -q '^PHAROS_DOMAIN=localhost$' "$target"
check "comments are kept" grep -q '^# --- Secrets' "$target"

first=$(cat "$target")
check "refuses to overwrite an existing file" bash -c "! bash '$root/scripts/init-env.sh' '$target' >/dev/null 2>&1"
check "the existing file is left untouched" test "$(cat "$target")" = "$first"

ln -s "$work/does-not-exist" "$work/dangling.env"
check "refuses to write through a dangling symbolic link" bash -c "! bash '$root/scripts/init-env.sh' '$work/dangling.env' >/dev/null 2>&1"
check "nothing was created behind the link" test ! -e "$work/does-not-exist"

(cd "$work" && bash "$root/scripts/init-env.sh" relative.env >/dev/null 2>&1)
check "a relative target is relative to the caller's directory" test -f "$work/relative.env"

second="$work/second.env"
bash "$root/scripts/init-env.sh" "$second" >/dev/null 2>&1
check "two runs generate different secrets" test "$(grep '^PHAROS_APP_PASSWORD=' "$target")" != "$(grep '^PHAROS_APP_PASSWORD=' "$second")"

if [ "$failed" -ne 0 ]; then printf '\n%s case(s) FAILED\n' "$failed"; exit 1; fi
printf '\nall cases passed\n'
