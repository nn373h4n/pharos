# 0022. Backup is a script run by the host, and a restore that is rehearsed

- Status: Proposed
- Date: 2026-11-04

## Context

Pharos keeps its state in PostgreSQL (articles, people, settings, the audit log, stored model usage)
and in `deploy/.env` (the root secret from which the audit HMAC key and the token keys are
derived). Redis holds sessions and queues; what losing it costs was not measured here, and it is
not part of this backup. There is no backup today (F-02), and the architecture proposal wants a
restore test, not only a dump. A dump nobody has restored is not a
backup.

The restore has two traps. The database uses several roles with exact privileges and row-level
security; a restore that skips the roles, or runs as the wrong role, produces a database that looks
right and leaks or fails. And a dump without `deploy/.env` cannot be read back to a working
installation.

## Options considered

1. **A script run by the host's scheduler** (`pg_dump -Fc` as the owner role, checked with
   `pg_restore --list`) — pros: no new long-running process, nothing added to the attack surface,
   easy to audit. Cons: the operator must set up the scheduler and the off-host copy.
2. **A resident backup container with its own schedule** — pros: more automatic. Cons: a new
   always-running service with database access and a copy destination to protect.
3. **Documentation only, with manual steps** — pros: least code. Cons: the restore cannot be
   repeated or tested, which is the point.

## Decision

Option 1.

- `scripts/backup.sh` writes a custom-format dump as the owner role, reads it back to its end with
  `pg_restore`, writes a SHA-256 file beside it, and rotates: 7 daily, 4 weekly, 3 monthly.
  `scripts/restore.sh` restores into a new database whose roles already exist (the init script made
  them on the new volume; the restore refuses a database without them, and roles that are not plain
  login roles). Both are plain shell, run on the host by its scheduler.
- The directories are the operator's and private; a path inside the repository, a link or a
  directory others can write to is refused.
- **A dump is code.** `pg_restore` runs it as the database owner, so a changed dump can weaken the
  tenant boundary (row-level security, a function with the owner's rights, an administrator).
  `restore.sh` copies the dump once into a private directory, refuses a checksum file that is
  missing, has more than one line, names another file or does not match, and reads only the copy.
  After the restore it looks for two things a good dump never makes (settings on the database or a
  role, and schemas other than `public`) and refuses extensions it does not know or that a
  superuser does not own, reading with names resolved in `pg_catalog` only. Then it compares the
  restored database with a reference that the `migrate` image's migrations build at the dump's own
  schema version in scratch databases on the same server, written through `pg_dump` and
  `pg_restore` like the dump (F-44): the database's grants, owners, grants, row-level security and
  policies, functions and their rights, triggers and the other objects in
  `scripts/restore/catalog.sql`. Any difference stops the restore. Rows are not compared, so hostile
  data can still pass. The checksum file protects against damage and against a change made by
  someone who cannot rewrite it too; keeping it, or authenticated encryption, out of the dump
  keepers' reach is the operator's part, and restoring only a dump whose origin is trusted stays
  the rule.
- The off-host copy and its encryption belong to the operator. The runbook says so and gives an
  example, but Pharos does not ship a destination.
- `deploy/.env` is backed up separately from the dump, because the root secret is needed to read
  the restored data (audit chain, tokens) and must not travel in the same place as the dump.
- The restore is **rehearsed**, not assumed: a throwaway stack is backed up, destroyed and restored;
  the schema version, the row counts of every table, row-level security, the policies, the exact
  grants of the two login roles, the functions, the triggers, the owners and grants of every
  relation, the schemas, the default privileges and the role attributes are equal; the services
  become healthy, `/readyz` is green and the administrator signs in again. The time is recorded.
  What the rehearsal does not show is listed in `docs/DEPLOYMENT.md`. The rehearsal on the pilot
  server is a gate (G8) that only the maintainer can pass.

## Consequences

- Positive: a backup that has been restored at least once; no new service; the secret and the data
  are stored apart.
- Negative: the operator owns the schedule, the off-host copy and the encryption; a missed
  scheduler is a missed backup, and Pharos does not alert on it.
- Follow-up: M5-02 builds the scripts, the rehearsal test and the runbook section; a report of a
  rehearsal on the pilot server names pilot details and stays out of the repository.

## References

- F-02 in `TASKS.md`; `docs/plans/m5-pilot-readiness.md` (M5-02, D3, gate G8).
- `docs/ARCHITECTURE_PROPOSAL.md`, question 10 and the restore test in the quarterly checks.
