# 0007. Embedding model and vector dimension

- Status: Proposed
- Date: 2026-10-26

## Context

Embeddings drive the cheap filter (similarity before any LLM call), semantic grouping of
duplicates and ranking. Content and users span four languages: Azerbaijani, Turkish, English and
Russian. A pgvector column has a fixed dimension, so changing the model affects the schema and
every stored vector. Small local models may be weak in Azerbaijani (risk R6), and the default
LLM is only chosen after an Azerbaijani evaluation; the same applies to embeddings.

## Options considered

1. **Pick a concrete model now** — pros: unblocks schema work immediately. Cons: no evidence
   of Azerbaijani quality; a wrong pick forces a full recompute later.
2. **English-only or translate-then-embed** — pros: widest model choice. Cons: an extra LLM
   call per item; translation errors distort similarity; cost grows with volume.
3. **Multilingual model required, chosen after evaluation; fixed dimension recorded in the
   schema; documented recompute procedure** — pros: decision based on measured quality;
   model changes are planned operations. Cons: schema and dedup threshold wait for the evaluation.

## Decision

Option 3. The concrete model and its dimension are **not chosen yet**. They will be decided after
the Azerbaijani evaluation and recorded by amending this ADR (or a superseding one) before it is
accepted. Until then no model name or dimension is fixed in code or docs.

Rules that hold regardless of the model:

- the model must be multilingual and cover az, tr, en and ru;
- `item_embeddings` stores `model` and `dim` with each vector; the vector column has one fixed
  dimension recorded in the schema;
- a model change is a full recompute: add the new model's vectors, backfill with an idempotent
  task, switch reads, then drop the old vectors; mixing models in one similarity query is invalid;
- dedup is clustering, not merging: items above a similarity threshold share a `cluster_id` and
  stay separate rows, so a false match never loses content. The threshold is tuned on an
  evaluation set;
- if the embedding provider is remote, calls go through the same gateway and egress rules as
  LLM calls ([0002](0002-llm-gateway-litellm.md)); a fully local option is an open question.

## Consequences

- Positive: no premature lock-in; model swaps follow a known, testable procedure.
- Negative: the `item_embeddings` migration and the cluster threshold are blocked on the evaluation.
- Follow-up: build the Azerbaijani evaluation set; record model, dimension and threshold here;
  tests that queries never compare vectors of different models and that recompute is idempotent;
  document the recompute runbook in `docs/DATA_MODEL.md`.

## References

- `docs/ARCHITECTURE_PROPOSAL.md` sections 2, 5 (`item_embeddings`), 6 (stages 3-4, quality and
  language), 11 (R6), 15, 16 (question 11)
- [0005 Tenancy and RLS](0005-tenancy-and-rls.md)
