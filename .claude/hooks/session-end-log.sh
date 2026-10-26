#!/usr/bin/env bash
# SessionEnd hook: append a one-line trace to today's session note, only if that note already exists.
# The note itself is created by the session-handoff skill, so this hook never creates a stub that
# would hide the last real handoff from the session-start skill.
[ -n "${CLAUDE_PROJECT_DIR:-}" ] && cd "$CLAUDE_PROJECT_DIR" 2>/dev/null || exit 0
note="docs/sessions/$(date +%F).md"
[ -f "$note" ] || exit 0
printf -- '- %s session ended on branch `%s`, %s changed file(s)\n' \
  "$(date +%H:%M)" \
  "$(git branch --show-current 2>/dev/null)" \
  "$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')" >> "$note"
exit 0
