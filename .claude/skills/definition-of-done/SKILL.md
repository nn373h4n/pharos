---
name: definition-of-done
description: Checks a task against the Pharos definition of done and produces the end-of-task report. Use before marking any task Done or opening a PR.
---

A task is not Done until every applicable line is true. Delegate test and lint runs to `test-runner` and the security pass to `security-reviewer`.

1. Acceptance criteria from TASKS.md verified — state how.
2. Tests written and run; result known per command (PASS / FAIL / NOT RUN).
3. Lint, format and type checks run.
4. Security and tenant boundary reviewed (`security-reviewer` for security-relevant changes).
5. New user-visible strings exist in all four locale files.
6. Docs updated where needed: ARCHITECTURE, DATA_MODEL, API, THREAT_MODEL, ADR.
7. Config and `.env.example` updated.
8. TASKS.md updated.
9. Failures, remaining risks and skipped work listed openly.

Then report to the user, in their language, with these headings:

- Result
- Files changed
- Checks (command — PASS / FAIL / NOT RUN)
- Security and risks
- Docs and tracking
- Decision needed / next step
