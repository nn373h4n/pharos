# M5 plan: hardening and pilot readiness

Status: **approved by the maintainer on 2026-11-04**, written the same day after M4. Nothing in this plan is built yet. The maintainer answered the decisions below on 2026-11-04 (see "Answers"); D1 (acceptance of the Proposed ADRs) and the open part of D9 (approved AI tools, raters) are still theirs, and the gates stay NOT RUN until people pass them. Sources: `docs/ARCHITECTURE_PROPOSAL.md` (milestone M5, sections 7, 9, 11, 12 and 14, question 10), `PROGRESS.md`, `TASKS.md` (M0 rows and the F-items), `docs/DEPLOYMENT.md`, `docs/THREAT_MODEL.md` (residual risks), `docs/guides/evaluation.md` and the M4 result.

## Goal

A tagged release, **v0.1.0**, that an operator can install on one server by following a runbook, back up and restore, keep from reaching hosts it should not reach, and run for 50 to 100 people of the pilot organization, with every claim about load, safety and quality either measured or written down as unmeasured. The critical path is pilot preparation, not code: M5 runs fully locally with Keycloak and the fake provider, while the gates that only people can pass run beside it.

Three kinds of work, kept apart in every section below:

- **(a) Tasks** the assistant builds and proves locally (tests, mutation checks, rehearsals in throwaway stacks).
- **(b) Gates** that only the maintainer or other people can pass (raters, the Entra ID tenant, the server, the key and budget, ADR acceptance). No code change, test or script run by the assistant marks a gate PASS.
- **(c) Things only the pilot can show**, listed under "will NOT prove".

## What M4 gives (and what it does not)

- A complete product path on the real stack with the fake provider: sources, ingestion, enrichment, the ranked feed, Try this, the budget with its 80% warning and 100% stop, the evaluation harness, the local model profile and near-duplicate clusters (both off by default).
- **Not** there: backup or restore of any kind (F-02); egress restricted by destination (F-04, F-06); a release, a tag, an SBOM, signed or digest-pinned images (F-05, F-07); a runbook or production checklist (`docs/DEPLOYMENT.md` still says so); retention defaults beyond the ingestion tables, and no way to delete one person's data; the approved AI tool list (F-35), which is part of the only P0 risk (R1).
- **Unmeasured**: Azerbaijani quality (F-36); translations (F-01); load beyond 3,000 synthetic articles; a worker killed in a real container; peak memory of the 10,000-entry feed in the stack; a real internet feed through the real worker; the Entra ID path against a real tenant; prompts and answers searched in the real stack's logs (M4 criterion 5 is PARTLY).

## Gaps and contradictions in the documents that the plan resolves

1. **The 30,000-user claim.** The proposal's summary says the monolith carries 30,000 users; section 9 says it is unproven until M5's load test. M5 measures a number on named hardware (D5). Until a run reaches 30,000 on comparable hardware, the summary is reworded to "estimate, not measured".
2. **Observability.** `docs/ARCHITECTURE.md` promises the `observability` profile (OpenTelemetry, Prometheus, Grafana) in M5 and says heartbeat metrics come with it; the self-hosting rule says no new service without a measured need. Nothing has measured that need yet (D7).
3. **Egress by destination cannot cover the worker.** Administrators and every person (personal feeds) can point the worker at any public host, and LiteLLM's HTTPS calls bypass `SafeHttpClient`. A host rule can deny private ranges, the bridge gateway and non-web ports for the worker and can allow-list the identity provider for the API; it cannot allow-list the worker's feed hosts. The plan says this plainly instead of claiming "default-deny egress". F-04 also lists `beat`, which has had no egress network since M3-03; its leftover is F-21.
4. **ADR-0016 says** the daily cap is set on the admin page and the budget events are audited; the code reads the cap from the environment and audits neither (F-39). D8 picks which one is corrected.
5. **Backup reports.** The proposal wants each quarterly restore test written to `docs/reports/`. Rehearsals on synthetic data may go there; a rehearsal on the pilot server names pilot details and stays out of the repository.
6. **Signing.** Today every installation builds its own images, so signing protects nobody until images are published (D4).
7. Stale lines to correct when the docs are touched: the `DEPLOYMENT.md` status line and "the API does not yet trust `X-Forwarded-*`" (done in M2, F-13); the ADR table in the proposal's section 15.

## Decisions needed from the maintainer (they block the tasks marked)

Answers of the maintainer, 2026-11-04 (the table below keeps the question and the recommendation):

- **D2** firewall rules and a check script, no new service. **D3** a script run by the host's scheduler, as recommended. **D4** images on GHCR with an SBOM and keyless signing; the signing tool is new in CI, so it is asked for again at M5-10. **D5** k6 as a pinned container run by a script. **D6** the proposal's defaults, plus a command that deletes one person's data. **D7** later; M5-14 is cut. **D8** the cap comes from the environment only; ADR-0016 is amended and the budget events go to the audit log. **D10** the cautious defaults: personal feeds off until gate G7, embeddings and the local model off, local login `break-glass`.
- **D1** the maintainer reads the Proposed ADRs and accepts them together. Not done yet; until then gate G1 is NOT RUN, and M5-03, M5-05 and M5-06 (which amend accepted text) wait for it. The assistant does not change an ADR status itself.
- **D9** the pilot professions are **SAP Basis and HR** (two, not three as in the M4 bar). The approved AI tools and the raters are still open. Note: the seeded profession list (migration 0002) has `hr_specialist` but no SAP Basis profession (it has `sap_consultant`); whether an administrator can add one was not checked. M5-01 settles this before the corpus is exported.

| # | Decision | Blocks | Recommendation |
| --- | --- | --- | --- |
| D1 | Accept or change the Proposed ADRs the pilot runs on: the ADR-0005 amendment, 0006, 0009 with 0019, 0010, 0011, 0012, 0013, 0014, 0015, 0016, 0017, 0018 | Gate G1, pilot start; M5-03, 05, 06 amend 0010, 0017, 0016 | Accept them before the M5 amendments, so each amendment changes an accepted text. 0007, 0020, 0021 are needed only if the pilot switches on embeddings or the local model (see D10) |
| D2 | Egress control: a host firewall rule set with a check script, or an outbound proxy profile (a new service) | M5-03 | **Firewall rules and a script, no new service.** API: identity provider and Graph on 443 only. Worker: public addresses on 80 and 443 only; no private ranges, metadata or bridge gateway. A proxy only if the pilot organization's IT requires one |
| D3 | Backup form and defaults | M5-02 | A script run by the host's scheduler (`pg_dump -Fc` as the owner role, checked with `pg_restore --list`), not a resident service; 7 daily, 4 weekly, 3 monthly; the off-host copy and its encryption are the operator's; `deploy/.env` backed up separately (the root secret derives the audit HMAC and token keys) |
| D4 | Release form: source tag only, or images published to a registry, signed, with an SBOM | M5-10 | Publish both images to GHCR with an SBOM from Trivy (already in CI) and keyless signing; the pilot pulls by digest. Signing is a new CI tool (ask first) |
| D5 | Load-test tool and targets | M5-08 | k6 as a digest-pinned container image run by a script (no product dependency). Targets: 30,000 synthetic articles, 300 synthetic people, 50 concurrent for 10 minutes, feed p95 under 400 ms, errors under 1%; then a stepped run that reports where p95 crosses 400 ms on the stated hardware; 100 SSO sign-ins in 2 minutes from one address |
| D6 | Retention defaults and person deletion (question 10) | M5-09 | The proposal's defaults: application log 30 days, audit 1 year, a person's interactions folded into counts after 12 months; an operator command that deletes one person's data and leaves an audit entry without content |
| D7 | The `observability` profile in M5 or later | M5-14 | **Later.** Pilot watching uses `/readyz`, the admin pages, log rotation and a status command (M5-11); correct `ARCHITECTURE.md`. Build the profile when the pilot shows a need |
| D8 | F-39: the cap from the environment (amend ADR-0016) or from the admin page | M5-06 | Environment only, amend the ADR; wire both budget events into the audit log |
| D9 | Still open from M4: the approved AI tools (M4 D5), the raters and their bar (M4 D6), the pilot professions (M4 D4; not recorded as decided in `TASKS.md`) | M5-07; gates G3, G4 | As in the M4 plan; the bar is not lowered |
| D10 | Pilot switches: personal feeds, embeddings, the local model, `PHAROS_LOCAL_LOGIN` | M5-11 | Personal feeds `off` until gate G7 passes on the pilot host; embeddings and the local model off unless F-36 tunes them; local login `break-glass` |

## Tasks (kind a)

Every row is one pull request with the M1 to M4 discipline: tests, `make lint`, `make test`, mutation checks for every new control, a `security-reviewer` pass before committing the risky ones (02, 03, 05, 06, 07, 09, 10), CI green. Migrations and new dependencies are asked for first. ADR column: a new or amended ADR (`Proposed`) is part of the task.

| Task | P | Title | Depends on | ADR | Acceptance |
| --- | --- | --- | --- | --- | --- |
| M5-01 | P0 | Plan copied in, M5 rows in TASKS.md, ADR drafts for backup, egress, release and retention | D1 to D10 answered | writes them | ADRs in the template as `Proposed`; no code |
| M5-02 | P1 | Backup and rehearsed restore (F-02) | 01, D3 | new | `scripts/backup.sh` and `scripts/restore.sh` (roles first, then data); a throwaway stack with synthetic data is backed up, destroyed and restored: equal row counts per table, RLS enabled on every tenant table, the exact-privilege tests pass against the restored database, sign-in and the feed work, `/readyz` green; time measured; a truncated dump is refused loudly; what Redis loses (sessions, the day's budget counters) is stated and tested |
| M5-03 | P1 | Egress control (F-04, F-06) and `beat` without a database URL (F-21) | 01, D2 | amends 0010 | Rule set plus `scripts/egress-check.sh` with a positive control per rule, proven in a disposable VM or network namespace, **never by changing the shared development host's firewall**; if no disposable host exists the check is NOT RUN and stays gate G7 |
| M5-04 | P2 | Network and HTTP leftovers: F-20, the request body limit of F-25, HSTS, F-17 | 01 | no | `PHAROS_PUBLIC_URL`'s addresses refused; DNS lookups bounded; `Settings` frozen; body limit at Caddy and in the app; HSTS in Caddy; F-17 checked first (does an unknown key id trigger a refetch?), then TTL and one deadline per provider call |
| M5-05 | P2 | Worker boundary: F-33 (delete and insert checks) and F-31 (first personal fetch an hour out) | 01, D1 | amends 0017; new for F-31 | Migration with downgrade; as the worker role, deleting a held article and inserting one as `new` over a held link fail; a new personal feed is not due for an hour; positive controls |
| M5-06 | P2 | Model layer leftovers: F-39, F-40 (a, b, c) | 01, D8 | amends 0016 | Budget events in the audit log; an insight is asked for by POST with CSRF and GET only reads; no insight for an article not yet read; a failing stage's pause lives in Redis |
| M5-07 | P1 | Approved AI tools and "Open in" (F-35) | 01, D9 | no | Admin-only list, empty by default (card offers Copy only); a link goes only to a listed https address; audited; four locales; axe 4 x 2 x 2; keyboard. Which tools: gate G9 |
| M5-08 | P1 | Load and resilience runs, and the M3 checks still NOT RUN | 02, D5 | no | D5 targets met or reported as FAIL with the numbers and the hardware; insight burst with the fake provider drains with no 5xx; a worker killed mid-fetch with `docker kill` leaves no duplicate and a second run adds 0; the 10,000-entry feed under 300 MB by the container's peak memory; the starter catalogue fetched once through the real worker from the real internet (manual, not CI; F-30 light) |
| M5-09 | P1 | Retention and person deletion | 01, D6 | new | Each default configurable and enforced by a job, tested at the boundary day; Docker log rotation in Compose; the audit log pruned only by an operator command as the owner role; deleting a person removes their profile, interactions, feeds and sessions and leaves one audit entry without content |
| M5-10 | P1 | Release process and v0.1.0 (F-05, F-07) | 02 to 09, D4 | new | Base images and `uv`/`pnpm` pinned by digest or hash, kept fresh by Dependabot; a tag workflow builds, scans, attaches an SBOM per image, signs (if D4), and the verify command in the release notes works; CHANGELOG cut to 0.1.0 with upgrade notes |
| M5-11 | P0 | Pilot runbook, production checklist, status command | 10, D10 | no | `docs/guides/pilot-runbook.md`: install, first sign-in (SSO and break-glass), switching sources on, key and budget, raising the budget, approved tools, what to watch each day, incidents (model off, personal feeds off), upgrade and rollback (previous tag plus restore), retention and deletion, end of the pilot; a participant briefing template on data safety (R1). The assistant installs v0.1.0 in a throwaway environment by the runbook alone and records the time |
| M5-12 | P2 | Reliable CI: F-41, and every suite that was NOT RUN | 01 | no | The F-41 file passes 20 runs in a row; `make test-sso-dev`, `make test-compose`, `make test-stack-e2e` green; prompts and answers searched in the real stack's logs (closes M4 criterion 5) |
| M5-13 | P0 | Whole-milestone security review, docs, readiness report | all | no | Acceptance below; the pilot-ready checklist filled honestly |
| M5-14 | P3 | `observability` profile | D7 = now | new | Only if D7 says so; first to cut |

Left on purpose, still open: F-08, F-09, F-10, F-11, F-12, F-16, F-19, F-22, F-23, F-24, F-26, F-27, F-28 (screen readers), F-29, F-34, F-42 (tuning is part of F-36), F-43 (development tools only; take it when `typescript-estree` allows minimatch 11). None blocks the pilot; each stays in TASKS.md.

## Gates (kind b): passed by people, never by a code change

| Gate | What passes it | Owner |
| --- | --- | --- |
| G1 | The ADRs of D1 accepted | Maintainer |
| G2 | Internal approval and the pilot teams (M0-01) | Maintainer |
| G3 | F-36: the evaluation bar met in Azerbaijani for each pilot profession, two native raters; default models set from the report (and in any other language offered to pilot users) | Maintainer and raters |
| G4 | F-01: az wording reviewed by a native speaker; tr and ru reviewed before those languages are offered | Maintainer and reviewers |
| G5 | Entra ID registration (M0-02) and the real sign-in checklist on the pilot server: sign-in, a person outside the assignment refused, a guest refused, department from Graph, sign-out, a disabled account stays out | Maintainer |
| G6 | Pilot server and domain (M0-03): v0.1.0 installed by the runbook, valid TLS, `/readyz` green | Maintainer |
| G7 | The egress rules applied on the pilot host and `scripts/egress-check.sh` PASS there | Maintainer or the pilot organization's IT |
| G8 | Backups scheduled on the pilot server, an off-host copy, one restore rehearsed there | Maintainer |
| G9 | Pilot key, budget and approved AI tool (M0-04) entered; "test the key" PASS | Maintainer |
| G10 | Professions and sources (M0-05): sources on, first fetches succeed, at least 20 cards for each pilot profession | Maintainer |
| G11 | Pilot participants briefed on data safety (R1) | Maintainer |

**Pilot ready** means every row below is PASS. Each row is reported as PASS, FAIL or NOT RUN; a gate that nobody ran is NOT RUN, never PASS.

| # | Check | Result |
| --- | --- | --- |
| 1 | M5 acceptance criteria 1 to 10 | NOT RUN |
| 2 to 12 | Gates G1 to G11 | NOT RUN |
| 13 | No open P0 or P1 (the proposal's G2 checkpoint) | NOT RUN |

## Acceptance criteria (measurable)

1. `make lint`, `make test`, `make test-integration`, `make test-compose`, `make test-stack-e2e`, `make test-sso-dev` green locally and in CI.
2. Each mutation makes a named test or check fail (recorded in the pull request): restore without the roles; skip the dump check; remove a firewall rule; drop the F-33 policy; make a new personal feed due at once; let GET queue an insight; skip a budget audit event; unpin a base image; an unsigned or tampered image passes verification (if D4); skip the retention job's boundary; leave a person's interactions after deletion.
3. The restore rehearsal of M5-02 passes, with its time recorded.
4. The egress check passes with positive controls on a disposable host (or is NOT RUN and carried to G7).
5. The load targets of D5 are met on the stated hardware, or the result is FAIL with the numbers; the resilience checks of M5-08 pass.
6. A fresh install of v0.1.0 by the runbook alone works in a throwaway environment; time recorded against the proposal's two-hour target.
7. Approved tools: Copy only by default; "Open in" only to a listed address; four locales, axe clean, keyboard.
8. Retention and deletion behave as D6 says, tested at the boundaries.
9. No secret, prompt or answer in the real stack's logs or audit log.
10. An independent whole-milestone security review has no open P0 or P1.

## What M5 will explicitly NOT prove

The pilot's "useful" ratio (60% or more), onboarding completion (90%), weekly active use and time saved; what real people paste into AI tools (R1 is reduced, not removed); the real daily cost; Azerbaijani quality beyond the rated professions and prompt versions; 30,000 users, unless a run reaches it on comparable hardware; stability over weeks; Entra ID in any directory but the pilot's; other identity providers in production; several organizations; screen readers (F-28); source terms of service; that the pilot host's firewall stays as configured.

## Order of work and the cut line

Order: 01; 12 (a trustworthy CI first); 02; 03; 05; 04; 06; 07; 09; 08; 10; 11; 13. The gates run beside the tasks and are the critical path: G3 and G4 need people and can take longer than all the code.

If time is short, cut in this order and write each cut into the release notes: M5-14; the stepped run of M5-08 beyond pilot size (the 30,000 wording stays "estimate"); image signing in M5-10 (keep SBOM and digest pins); the 12-month folding of interactions in M5-09; M5-04 except F-20. Not cuttable: 02, 03, 07, 08 at pilot size, 10 (tag, SBOM, pins), 11, 13. Cutting M5-05 needs the maintainer's explicit acceptance of F-33 as a known risk. A failed gate is never "cut": the pilot waits.

## Risks

| Risk | Handling |
| --- | --- |
| R1 sensitive data pasted through "Try this" (P0) | Approved tools (M5-07), the data note, placeholders, the briefing (G11); stays open as a behaviour risk |
| R6 Azerbaijani output fails the bar | Try another model or prompt version and rate again; offer fewer languages; never lower the bar |
| R2 pilot preparation (approval, Entra, server, key) | Gates start now; code waits for none of them |
| The pilot host's network belongs to the pilot organization's IT | The rule set and check script are written so IT can apply and rerun them (G7) |
| Losing `deploy/.env` makes a backup partly useless | The runbook backs it up apart from the dump |
| Sign-in burst behind one shared address meets the 120-a-minute SSO limit | Measured in M5-08; the limit is a setting, changed only with numbers |
| Load numbers from the development host do not match the pilot server | Every number carries its hardware; G6 may repeat the run |
| Firewall tests on a shared development host | Only in a disposable VM or namespace, otherwise NOT RUN |
| Schedule | M3 took about three days and M4 about two, with review rounds; that is no basis for M5, whose length is set by the gates. No dates are promised; the cut line is the control |

## Result
