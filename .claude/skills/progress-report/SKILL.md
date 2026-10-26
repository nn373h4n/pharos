---
name: progress-report
description: Weekly or milestone progress report into PROGRESS.md and docs/reports/.
disable-model-invocation: true
---

Delegate to the `docs-keeper` subagent with this brief:

- Verify status from TASKS.md, `git log` since the last report and the latest CI results. Do not rely on memory.
- Update PROGRESS.md: current milestone; done, in progress, blocked; CI status; top risks; decisions needed from the user; next 3–5 steps.
- Completion percentage only from counted tasks; label estimates.
- Write `docs/reports/YYYY-Www.md` (max 60 lines).

Then summarize the report to the user in 5 lines.
