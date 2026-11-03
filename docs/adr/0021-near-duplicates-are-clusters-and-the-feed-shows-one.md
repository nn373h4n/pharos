# 0021. Near-duplicates are clusters, and the Home feed shows one card for a story

- Status: Proposed
- Date: 2026-11-03

## Context

The same story reaches the feed through several sources (a syndicated article, a press release
copied by three outlets, a feed and a Hacker News post). Exact deduplication (M3-08) catches only
the same address. The Home feed would show one story several times, which wastes the little time
a person gives it and buries other cards. ADR-0007 decided that dedup is clustering, not merging,
and left the model, the vector size and the threshold open. M4-05 builds the mechanism; the
operator chooses the model.

## Options considered

1. **Merge near-duplicates into one row.** Pros: simple feed. Cons: a wrong match loses an article
   for ever and its sources with it.
2. **Cluster and hide in the feed query (chosen).** Pros: nothing is lost, the cluster can be
   undone by clearing a column, editors still see every article. Cons: the feed query is a little
   heavier.
3. **Collapse in the application after the query.** Pros: no SQL change. Cons: breaks paging by
   cursor (a page could come back short or repeat a story).
4. **A vector index and a nearest-neighbour search over all articles.** Pros: scales to millions.
   Cons: needs a fixed vector size in the schema (the model is not chosen) and is not needed for
   the pilot size; articles are compared only within a window of recent days.

## Decision

Option 2.

- `item_embeddings(org_id, item_id, model, dim, embedding)`, one vector per article and model. The
  column has no fixed size; each row carries the model and its size and a check keeps them honest.
  Queries compare only vectors of the same model and size, so a model change is a recompute and no
  row of the old model is ever compared with the new. Only the worker reads and writes the table;
  the API and the application role cannot see a vector.
- `items.cluster_id` joins near-duplicates. A worker sweep, every minute, embeds the public,
  current articles that the model has read (class A text only, masked, booked and recorded like any
  other model call, stage `embedding`), stores the vector and looks for the nearest article of the
  last `PHAROS_EMBEDDING_WINDOW_DAYS` days. At or above `PHAROS_EMBEDDING_SIMILARITY` the two share
  a cluster. Placing an article takes a lock per organization, so two sweeps cannot cluster the same
  neighbours at once. An article that already has a cluster keeps it: clusters are never merged.
- The Home feed query shows, of the members of a cluster that could be shown, only the one seen
  first (when two were stored at the same moment: higher quality, then the lower identifier). If
  it cannot be shown (held, hidden, outside the window) the next takes its place. A story a person
  marked "not useful" is hidden with its copies. Paging is unchanged. Quality does not come first
  on purpose: it is model output over text a source wrote, so a syndicated copy that talks the
  model into a high score would otherwise replace the original's link for every reader (found in
  the review of M4-05). A copy joined later therefore never displaces an earlier one.
- A cluster has at most 20 articles, whichever article the join starts from, and a newcomer must be
  close to the cluster's first member as well as its nearest one.
- The default threshold is conservative (0.92 cosine): a missed copy shows a story twice, a wrong
  match hides an article behind another. It is tuned per installation with
  `python -m pharos.evaluation dedup` on pairs of articles a person has labelled.
- Embeddings are off unless `PHAROS_EMBEDDING_PROVIDER` is set (`ollama`, `openai`, `azure` or the
  test provider `fake`, which is refused in production). `openai` and `azure` use the chat
  provider's key and must be the chat provider; the Anthropic chat provider offers no embeddings,
  so its installations use `ollama`. The model name is the operator's and is not in the code.
- What was measured, and what was not. One real model was tried (`bge-m3`, served by the local
  model server, 1024 dimensions) on 14 pairs of articles in English, Azerbaijani, Turkish and
  Russian that the assistant wrote and no native speaker checked. At 0.80 every same-story pair was
  joined and no different story was; at 0.85 one pair was missed; at 0.90 six of seven were missed;
  at the default 0.92 **none** was joined, so with that model the default does nothing until it is
  tuned. That is a small sample, not a recommendation: the threshold must be tuned on the
  installation's own articles (`dedup`), and nothing is claimed for any language or other model. The
  default stays cautious because a wrong join hides an article and a missed one only shows a story
  twice. The test provider hashes words and says nothing about meaning.

## Consequences

- Positive: one card for one story; nothing lost; the mechanism is testable end to end with the
  test provider; a model or threshold change needs no schema change.
- Negative: a wrong match still hides an article from the Home feed (not from editors or the
  article list); vectors cost storage (a few KB per article); the nearest-neighbour search is a scan
  over the vectors stored in the last window (no vector index), fine for the pilot and to be
  replaced by an index if it ever is not.
- Follow-up: run `dedup` on real labelled pairs and record the chosen model and threshold
  (F-36); an index when volume needs it.

## References

- ADR-0007 (model and dimension), ADR-0015 (what may reach a model), ADR-0016 (budget),
  ADR-0018 (the feed), ADR-0020 (the local model); `docs/plans/m4-llm-layer-and-personal-feed.md`
