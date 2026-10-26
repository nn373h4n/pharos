#!/usr/bin/env bash
# PreToolUse hook for Bash: forwards the hook JSON to the Python guard.
# -I isolates the interpreter from the current directory and user site.
# Fail closed: only the guard's own exit 0 allows the command, any other code blocks.
python3 -I "$(dirname "$0")/pre_bash_guard.py"
status=$?
[ "$status" -eq 0 ] && exit 0
[ "$status" -eq 2 ] || echo "pre-bash-guard: guard failed (exit $status); blocking" >&2
exit 2
