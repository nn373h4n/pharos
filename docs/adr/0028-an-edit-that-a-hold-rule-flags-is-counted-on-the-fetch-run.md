# 0028. An edit that a hold rule flags is counted on the fetch run

- Status: Accepted (by the maintainer, 2026-11-14)
- Date: 2026-11-14

## Context

When a source changes the title or excerpt of an article it reported earlier, the worker replaces
the item's text (M3-08). Text that a hold rule flags (a link to a numeric host, an unusual port, a
phrase that addresses a model) never replaces the text of an item that exists: a clean, perhaps
approved, item must not turn into a flagged one without a person seeing it. The worker cannot hold
it either. The database lets the worker change `status` only for an organization article that is
new, unread by the model and undecided by a person, and then only to hold it as `low_quality`
(migration 0006, `items_worker_only_holds`). An edit like that is therefore dropped without a
trace: the item keeps its old text, and nobody learns that the source began to send flagged text
(F-24, found in the security review of M3-08).

Nothing hostile reaches a reader or the model this way, because the flagged text is not stored.
What is lost is a signal: a source that edits its articles into text addressed to a model has
probably been taken over, and the person who decides whether to keep the source never hears of
it.

## Options considered

1. **Keep the flagged text aside for review** (an `item_pending_texts` table, written by the worker,
   shown on the review screen with "accept" and "dismiss"). This shows the reviewer exactly what
   changed. Cons: a new table, new API routes, new screens in four languages and a new kind of
   decision; it stores hostile text that today is never stored; and the decision a person really
   has to make is about the source, not about one article.
2. **Let the worker hold an item that was already reviewed** (relax `items_worker_only_holds`).
   This takes away the guarantee that a person's decision is never undone by the process that
   handles hostile content (ADR-0012), so a compromised worker could hide any approved article.
   Refused.
3. **Mark the item** (`items.content_changed_at`, a column the worker may set). The review list
   could filter on it. Cons: the mark is on the article while the signal is about the source; the
   reviewer cannot see what changed; and the worker gets a new way to change approved rows.
4. **Count the dropped edits on the fetch run** (`fetch_runs.edits_refused`, an integer the worker
   writes with the other counts of the run). The Sources page shows it in the run history next to
   "seen" and "new", and the source card warns when the last run refused any edit. Pros: one
   column, written where the worker already writes; no hostile text stored; the person who looks
   after the source sees it where they already look; no item row and no decision changes. Cons:
   the reviewer does not see the refused text (the source's own page does), and an administrator
   who never opens the Sources page does not see the warning.
5. **Keep it as it is and document it.** The cost is nothing; the signal stays lost.

## Decision

Option 4, as built in migration 0012 (2026-11-14).

- Migration: `fetch_runs.edits_refused integer NULL CHECK (edits_refused >= 0)`. The worker may
  write it in the same `UPDATE` as `items_seen` and `items_new` (the grant is widened by that one
  column); the application role reads it. No change to `items`, to its triggers or to any grant on
  it.
- `pipeline.store_items` returns how many existing items kept their text because the new text was
  flagged (the conflict rows that the `EXCLUDED.status = 'new'` condition skipped, counted from the
  candidates that are held and already existed). The fetcher writes the number on the run.
- API: `GET /api/sources/{id}/runs` and `GET /api/me/feeds/{id}/runs` return `edits_refused`
  (`null` for a run that fetched nothing new, and for runs before the migration).
- Front end: a column in the run history, and a note above it when any of the fetches shown
  refused an edit ("N edits were refused in these fetches: a hold rule flagged the new text. The
  articles keep their earlier text; check the source."), in four languages. As built, the note is
  in the run history of a source or a personal feed, not on the card itself: the source list does
  not carry the runs, and adding them was not worth a second query on every list.
- Audit: none. The count is operational data about a source and lives as long as the run (30 days).
- Documentation: DATA_MODEL, API, THREAT_MODEL (the F-24 residual risk shrinks to "the reviewer
  does not see the refused text").

## Consequences

- A source that is taken over and starts editing its articles into flagged text is visible on the
  Sources page within one fetch.
- The flagged text is still never stored, so there is nothing new to protect, retain or delete.
- The guarantee of ADR-0012 stays whole: the worker never changes an article a person has decided
  about.
- One migration, one grant change and two new strings in four languages.
- An administrator sees the note only when they open the run history of a source.
- If the pilot shows that reviewers need the refused text itself, option 1 can be added later on
  top of the count.

## References

- [0012 Worker identity and ingestion boundaries](0012-worker-identity-and-ingestion-boundaries.md)
- `backend/src/pharos/ingest/pipeline.py` (`store_items`), migration 0006
  (`items_worker_only_holds`), F-24 in `TASKS.md`
