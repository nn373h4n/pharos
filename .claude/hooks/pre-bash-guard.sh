#!/usr/bin/env bash
# PreToolUse hook for Bash: forwards the hook JSON to the Python guard.
# -I isolates the interpreter from the current directory and user site.
exec python3 -I "$(dirname "$0")/pre_bash_guard.py"
