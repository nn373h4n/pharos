#!/usr/bin/env bash
# PostToolUse hook for Edit|Write: format the edited file when the formatter is already installed.
# Never installs or syncs anything and never blocks; a missing tool or a formatter error is only reported.
root=$(cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null && pwd -P) || exit 0
file=$(jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -n "$file" ] && [ -f "$file" ] || exit 0
file=$(realpath "$file") || exit 0
case "$file" in "$root"/*) ;; *) exit 0 ;; esac # only files inside the project

case "$file" in
  *.py)
    if command -v uv >/dev/null && [ -f "$root/backend/pyproject.toml" ]; then
      (cd "$root" && uv run --no-sync ruff format -- "$file") >&2
    fi
    ;;
  "$root"/frontend/*.ts | "$root"/frontend/*.tsx | "$root"/frontend/*.js | "$root"/frontend/*.jsx | "$root"/frontend/*.json | "$root"/frontend/*.css)
    prettier="$root/frontend/node_modules/.bin/prettier"
    [ -x "$prettier" ] && "$prettier" --write "$file" >&2
    ;;
esac
exit 0
