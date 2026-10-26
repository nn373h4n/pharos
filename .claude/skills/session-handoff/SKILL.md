---
name: session-handoff
description: End-of-session handoff that keeps project memory short and accurate. Use when the user wraps up, before /clear after a finished task, or when the context is getting long.
---

1. Write or extend `docs/sessions/YYYY-MM-DD.md` (max 40 lines per session):
   - Goal
   - Done, with evidence (command — PASS / FAIL / NOT RUN)
   - Files changed
   - Decisions (link ADRs)
   - Open issues and the single next step
2. Delegate TASKS.md status updates to `docs-keeper` with a short summary. Update PROGRESS.md only if a milestone changed.
3. If you learned a durable project convention, propose a one-line addition to the matching `.claude/rules/` file or to CLAUDE.md and ask the user. Do not edit CLAUDE.md yourself.
4. No secrets, company names or pilot data.
5. Tell the user the session is saved and `/clear` is safe now.
