# 0025. Retention defaults and deleting one person's data

- Status: Proposed
- Date: 2026-11-04

## Context

Question 10 of the architecture proposal asks how long data is kept and how a person's data is
removed. Today only the ingestion tables have retention. There is no way to remove one person's
data, container logs have no rotation by default, and the audit log has no stated lifetime. For a
pilot of 50 to 100 people inside one organization this has to be decided before real people use the
product, and it must not depend on reading the code.

## Options considered

1. **The proposal's defaults, configurable, with an operator command to delete one person** —
   pros: concrete, testable at the boundary day, no surprise for the pilot organization. Cons: the
   defaults may not match that organization's policy; they can be changed.
2. **Shorter periods** (for example logs 14 days, audit 6 months) — pros: less data held. Cons:
   a shorter audit trail weakens investigations.
3. **Wait for the pilot organization's policy** — pros: the right numbers. Cons: blocks the
   mechanism, which is needed whatever the numbers are.

## Decision

Option 1.

- Defaults: application log 30 days, audit log 1 year, a person's interactions folded into counts
  after 12 months. Each is a setting and is enforced by a job, tested at the boundary day.
- Docker log rotation is set in Compose.
- The audit log is pruned only by an operator command run as the owner role, never by the
  application.
- An operator command deletes one person's data (profile, interactions, personal feeds, sessions)
  and writes one audit entry that has no content of the person.
- The numbers are defaults, not a promise: the pilot organization's own policy replaces them by
  setting.

## Consequences

- Positive: a stated lifetime for each kind of data and a way to honour a request to delete.
- Negative: folding interactions into counts loses the per-person history after 12 months (this is
  the intended privacy effect, see ADR-0018); deleting a person does not remove aggregate counts
  that never held their identity.
- Follow-up: M5-09 builds the jobs, the command and the tests (boundary day, a deleted person leaves
  one contentless audit entry, other people's data untouched); a migration may be needed and is
  asked for first; the runbook gets a retention and deletion section.

## References

- `docs/ARCHITECTURE_PROPOSAL.md` question 10; ADR-0018 (interactions are private and counted in
  aggregate); `docs/plans/m5-pilot-readiness.md` (M5-09, D6).
