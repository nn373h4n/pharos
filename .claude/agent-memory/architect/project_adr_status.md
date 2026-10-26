---
name: project-adr-status
description: Which of ADR 0001-0009 are Accepted vs Proposed and why; source of truth for status and open items
metadata:
  type: project
---

As of 2026-10-26: ADR 0001, 0002, 0003, 0004, 0005 and 0008 are Accepted. 0006, 0007 and 0009 are
Proposed and are revisited in M3/M4.

**Why:** 0003, 0005 and 0008 follow accepted decisions 3, 2 and 7 in ARCHITECTURE_PROPOSAL.md. The maintainer
accepted 0001, 0002 and 0004 on 2026-10-26 during M1 planning. Only the maintainer accepts ADRs.

**How to apply:** do not flip a status without the maintainer's own confirmation. 0007 is blocked
until the Azerbaijani embedding evaluation picks a model and dimension; never invent them.
0005 supersedes the master-prompt rule "tenant in every cache key" (public data has no tenant).
