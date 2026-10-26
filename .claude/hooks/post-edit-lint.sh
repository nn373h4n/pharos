#!/usr/bin/env bash
# PostToolUse hook for Edit|Write: format the edited file when the formatter is installed.
# Never installs anything and never blocks; a missing tool or a formatter error is only reported.
file=$(jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -n "$file" ] && [ -f "$file" ] || exit 0

case "$file" in
  *.py)
    command -v uv >/dev/null && [ -f "${CLAUDE_PROJECT_DIR:-.}/backend/pyproject.toml" ] \
      && (cd "${CLAUDE_PROJECT_DIR:-.}" && uv run --offline ruff format "$file") >&2
    ;;
  *.ts | *.tsx | *.js | *.jsx | *.json | *.css)
    [ -x "${CLAUDE_PROJECT_DIR:-.}/frontend/node_modules/.bin/prettier" ] \
      && "${CLAUDE_PROJECT_DIR:-.}/frontend/node_modules/.bin/prettier" --write "$file" >&2
    ;;
esac
exit 0
