#!/usr/bin/env bash
# SessionEnd hook: append a one-line trace to today's session note.
# The full handoff is written by the session-handoff skill; this only records that a session ended.
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
note="docs/sessions/$(date +%F).md"
mkdir -p docs/sessions
[ -f "$note" ] || printf '# Session log %s\n\n' "$(date +%F)" > "$note"
printf -- '- %s session ended on branch `%s`, %s changed file(s)\n' \
  "$(date +%H:%M)" \
  "$(git branch --show-current 2>/dev/null)" \
  "$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')" >> "$note"
exit 0
