# 0017. The worker writes what the model said, and may hold an article as low quality

- Status: Proposed
- Date: 2026-11-01

## Context

M4-04 has a model read each public article and say what it is: a short summary, a category, a
quality score, and how well it fits each profession. The worker makes the call (ADR-0004,
ADR-0015), so the worker has to write the answer. Until now the worker could update only the text
columns of `items` (migration 0003), and it could not change the state of an article at all:
whether an article is `new`, `held` or `hidden` is a person's decision (ADR-0012), and a machine
must not undo it.

An article the model finds poor, off topic, or whose answer looks like it was steered by the
article, should not reach the feed. Today only an editor can hold an article, so such articles
would wait in the feed until someone looks.

## Options considered

1. The worker writes the answer and the API holds the article later. — There is no later: the API
   does not run on a schedule, and a poor article would show until someone opens the queue.
2. The worker gets the right to change `status` freely. — It could release a held article or
   restore a hidden one. A taken-over worker (it handles hostile content) could publish what an
   editor refused.
3. The worker writes the answer and may move an article from `new` to `held` with the reason
   `low_quality`, and nothing else; a trigger enforces it. — A machine can only make the product
   more careful, and only on an article no person has decided about yet.
4. A restrictive row-level security policy instead of a trigger. — The ingestion path updates the
   text of held rows as well, and with row-level security the update of a row that fails the
   policy is an error, not a skipped row. A trigger on the `status` and `hold_reason` columns
   leaves the text updates alone.

## Decision

Option 3, with the trigger of option 4's reasoning. Migration `0006`:

- `items` gains `summary`, `category`, `quality`, `enriched_at`, `enrichment_version` and
  `reviewed_at`. The application sets `reviewed_at` when a person holds, approves, hides or
  restores an article, so "a person has decided" is remembered even when the article is `new`
  again. The five answer columns come with
  checks (the three answer columns come together, the time and version come together, a summary is
  never without them, the category is one of eight words, the quality is 1 to 5).
- `item_professions(org_id, item_id, profession_code, relevance)` holds the fit to each profession,
  under the tenant policy, with the personal-rows policy of its article.
- `pharos_worker` gets `UPDATE` on those five columns and on `status` and `hold_reason`, `INSERT`
  and `SELECT` on `item_professions`, and `SELECT (code, scope, owner_org_id)` on `professions`
  (the existing policy shows it public rows and its own organization's) so the model's answer can
  be limited to codes that exist.
- A `BEFORE UPDATE` trigger on the state and answer columns lets `pharos_worker` change them only
  on an organization article that is `new`, that no person has decided about (`reviewed_at` is
  empty) and that has not been read yet (`enriched_at` is empty), and then only to write the answer
  or to hold it as `low_quality`. Every other change by the worker raises an error. The application role and the table owner are not limited by it.
- The article is read only by `load_public_article` (ADR-0015); the sweep selects public, `new`,
  unread articles, and writing the answer is guarded by the same conditions, so a decision an editor
  made while the model was working stands.

This replaces ADR-0012's "the worker cannot change the state of an article" for this one
transition. An editor approves a held article as for any other hold.

## Consequences

- A poor, off-topic or suspicious article is held without a person; a person can approve it.
- A taken-over worker can hold new, unreviewed, unread articles (a nuisance, visible in the editor
  queue, reversible) and can write false summaries to those same articles. By `UPDATE` it cannot
  publish, hide or restore anything, cannot write onto a held, hidden, reviewed, already read or
  personal article, and the model path (the sweep and `load_public_article`) refuses all of those
  too.
- A limit that remains from migration 0003, not widened here: the worker may `DELETE` an article
  that is not hidden (retention) and `INSERT` one as `new` or as `held` with any reason (ingestion),
  so a taken-over worker could delete a held article and insert it again as `new`. The trigger
  does not see that. Tracked as F-33. Summaries are shown as plain text only
  (M4-04b), and the answer is checked for links and markup before it is stored.
- The exact-set privilege tests (`test_worker_role.py`, `test_ingestion_hardening.py`) and
  `tests/integration/test_item_enrichment.py` pin all of the above, including every transition the
  worker must be refused.
- Follow-up: M4-04b shows the fields; the prompts are versioned (`enrichment_version`), and a new
  version does not rewrite old answers in bulk.

## References

ADR-0004, ADR-0012, ADR-0014, ADR-0015, ADR-0016; `docs/plans/m4-llm-layer-and-personal-feed.md`
(M4-04); migration `0006_item_enrichment`.
