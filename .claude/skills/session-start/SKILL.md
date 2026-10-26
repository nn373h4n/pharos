---
name: session-start
description: Short start-of-session briefing from the latest session note and TASKS.md. Use at the start of a work session or when the user asks where we left off.
model: haiku
---

Brief the user without exploring the whole repository:

1. Read only the newest file in `docs/sessions/`.
2. Read `TASKS.md`: list In Progress and Blocked items and the next three Todo items by priority.
3. Run `git status --short` and `git log --oneline -5`.
4. Reply in at most 15 lines: last outcome, blockers, proposed next task. Ask the user to confirm the task before starting.

Do not open other documents until a task is chosen.
