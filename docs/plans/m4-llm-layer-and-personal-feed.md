# M4 plan: the LLM layer, enrichment and the personal feed

Status: **draft for the maintainer's approval**, written 2026-10-31 after M3. Nothing in this plan is built. Decisions D1, D2, D3 and D7 were answered by the maintainer on 2026-10-31 (marked "decided" below); D4, D5, D6, D8 and D9 are still open. Sources: `docs/ARCHITECTURE_PROPOSAL.md` (sections 5 and 6, milestone M4, acceptance rows, questions 8, 9 and 11), `docs/PLAN.md`, `PHAROS_MASTER_PROMPT.md` section 7, ADR-0002, 0004, 0005, 0007, 0009, and the M3 result.

## Goal

A person signs in, has answered the onboarding questions, and sees a **feed of cards** ranked for their profession, interests and AI level. Each card has a short summary in their language, "how this applies to your work", and a "Try this" prompt with a data-safety note. Every model call goes through one internal `LLMGateway`, has a budget, is checked against a schema and can be switched off. People can say "useful" or "not useful".

## What M3 already gives (and what it does not)

- Articles exist as `items` (title, excerpt of at most 500 characters, link, `lang`, `status`), per organization, with personal items per person (M3-13). The feed reads `items`; it needs no new fetching.
- There is **no** `llm` package, no LLM setting, no LiteLLM or pgvector Python dependency, no `item_*` enrichment table, no `interactions` or `llm_usage` table, and no fake LLM provider. `pgvector` is created in the database (`deploy/postgres/init/01-roles.sh`) but no migration uses it.
- The Home page does not show articles yet.

## Gaps and contradictions in the documents that the plan resolves

1. **"Personal feed"** in the proposal means a ranked feed of cards for one person. It is not the personal RSS of M3-13 (done). This plan uses "feed" for the ranked cards.
2. **Public content plane.** ADR-0005 and the proposal's data model assume `scope = public` content with a shared insight cache keyed by (item, profession, language, prompt_version). M3 built per-organization items with no `scope`. Decision (ADR-0015, below): one installation serves one organization (ADR-0005), so the cache is keyed as the proposal says and lives next to the organization's items; a shared public plane is a later step, not M4.
3. The proposal's ADR table lists ADR-0002 as proposed; the file says accepted. The plan treats it as accepted and fixes the table.
4. `docs/ARCHITECTURE.md` says cloud egress "is closed" with Ollama; that is a claim nobody can prove (principle 8). It is reworded when the local profile exists.
5. The cost figure (600 insight calls a day) is an unvalidated assumption. No budget number exists anywhere. Budgets are decided by the maintainer before M4-02 ships defaults.

## Decisions needed from the maintainer (they block the tasks marked)

| # | Decision | Blocks | Recommendation |
| --- | --- | --- | --- |
| D1 | Provider and default models per stage (cheap filter, enrichment, insight), and who holds the key (M0-04) | M4-02, 03, 09 | **Decided: Anthropic (Claude) first.** The gateway stays provider-neutral (LiteLLM), tests use the fake provider, and the model per stage is chosen from the Azerbaijani evaluation (M4-09), not before |
| D2 | Which data may go to a cloud model, and what is masked | M4-02, 04 | **Decided: public article text only** (title, excerpt, link host); never anything a person typed, never a personal feed's articles; masking of e-mail, phone, IBAN, national ID and names is built into the gateway for any future company content (ADR-0015) but company content is not sent in M4 |
| D3 | Monthly budget per installation, the 80% warning and the 100% stop | M4-02 | **Decided: a small default cap (a few USD a day), the administrator raises it** after the pilot shows the real cost; shown in the admin page; 80% warns, 100% stops new generation and keeps the cache |
| D4 | Pilot professions (question 8, three to five roles per team) and the card format (question 9) | M4-04, 06, 08, 09 | Finance: accountant, payroll specialist; HR: HR generalist; SAP: FI/CO consultant, Basis administrator. Card: 2-sentence summary, 3 steps to apply, 1 prompt, 1 data note |
| D5 | The approved "Try this" tool list (M0-04) and its wording | M4-08 | An admin-edited list; empty by default, so the card shows only "copy" until the admin adds a tool |
| D6 | Who rates the Azerbaijani output, how many items, what bar | M4-09 | Two native speakers, 30 to 50 items per pilot profession, mean at least 4 of 5 (the proposal's bar); a reviewer also covers F-01 |
| D7 | Fully local mode (question 11) in M4 or later | M4-10 | **Decided: later.** M4-10 is built only if time remains; no "fully local" claim before it passes the same evaluation |
| D8 | New dependencies: `litellm`; optionally the `pgvector` Python package | M4-02, 05 | `litellm` yes (ADR-0002). Skip the `pgvector` package: use SQL with text casts and an ADR-0007 decision |
| D9 | Accept the proposed ADRs this milestone depends on: 0007 (embeddings), 0009 (output sanitization and CSP), 0014 | M4-05, 06, 08 | Accept 0009 with F-03 (strict CSP) done before the cards render model output |

## Tasks

Order: 01, then 02 to 04 in sequence, 05 and 10 beside them if time allows, 06, 07, 08, 09, 11. Each pull request keeps the M1 to M3 discipline: tests first-class, `make lint`, `make test`, mutation checks for every new control, a `security-reviewer` pass before committing the risky ones (02, 03, 04, 06, 08), CI green.

| Task | Content | Notes and risk |
| --- | --- | --- |
| M4-01 | ADR-0015 (what may go to a model, masking, content plane, cache key) and ADR-0016 (budget and cost accounting); accept 0007 and 0009 as decided; F-03 strict CSP started | Docs only; the decisions D1 to D9 are written down here |
| M4-02 | `pharos.llm`: `LLMGateway` over the LiteLLM SDK (the only importer of `litellm`, enforced by an import-linter contract); stage to model map; timeouts and retry with backoff; pydantic schema validation of every answer; no tools, ever; telemetry without content; fake provider and recorded fixtures for tests; migration 0005 `llm_usage`; budget in Redis and the table, 80% warning, 100% stop; settings and `.env.example` | Highest risk: this is the boundary. Mutations: remove the budget check, the schema check, the timeout, the "no tools" argument, the content-free logging |
| M4-03 | Admin API and page for provider, model per stage, key (encrypted with a purpose-derived key, rotation by `key_version`), "test the key", budget and usage; audit | The key is never returned, logged or sent to the browser |
| M4-04 | Enrichment: migration for `item_professions`, quality score and `held` reason `low_quality`; rule filter, then the model (summary, category, profession relevance, quality) with delimited data, schema-checked JSON, link allow-list (only the item's own host); idempotent worker tasks; low quality or a suspicious answer goes to the editor queue | Prompt-injection corpus (hostile article text) with expected outputs; the product's model has no tools and its answer is never executed |
| M4-05 | Embeddings and clustering: `item_embeddings`, similarity grouping into `cluster_id` | **Cut line**: if time runs out, ranking uses rules and the model's relevance only; the duplicate rate stays under 5% by URL and title rules |
| M4-06 | Insights: migration `item_insights` unique on (item, profession, language, prompt_version); generated lazily and in the background when a card is first shown, in the person's language (az, tr, ru, en) in the same call; `try_prompt` validator (no links, no request for passwords or personal data, length limit, placeholders instead of real data); old versions refreshed when viewed | Translation quality is judged by D6, not by the model |
| M4-07 | Feed API: ranking by profession relevance, interests, AI level (affects ranking and tone, not the cache key), recency and quality; cursor pagination; at least 20 cards on first sign-in; p95 under 400 ms on the pilot data size (a load test); `interactions` (view, save, useful, not_useful, tried, time_saved) with aggregate-only counting | Per-organization data only; all queries filtered by `org_id` and the person; RLS stays the second line |
| M4-08 | Front end: the Home feed of cards (summary, how it applies, "Try this" with copy and the approved tools, data note on every card), feedback buttons, save; admin pages (LLM settings from M4-03, approved tools); four locales; axe; keyboard only | Model output is rendered as plain text; links only to the item's own host; strict CSP (F-03) before this ships |
| M4-09 | Evaluation harness: the rating sheet, import of ratings, a script that reports mean score per profession and language, the `prompt_version` it applies to; the first run chooses the default models | Needs human raters (D6); no automated judge counts toward the bar |
| M4-10 | `local-llm` Compose profile with Ollama and a local embedding model; the admin choice; wording of the egress claim | **Cut line** per D7; no claim of "fully local" until it passes the evaluation |
| M4-11 | Real-stack E2E with the fake provider (cards appear, budget stop keeps the cache, hostile article text cannot change the output's structure or links), whole-milestone security review, docs (API, DATA_MODEL, THREAT_MODEL, ARCHITECTURE, DEPLOYMENT, guides), PROGRESS, session note | Acceptance below |

## Acceptance criteria (measurable)

1. `make lint`, `make test`, `make test-integration`, `make test-compose`, `make test-stack-e2e` green locally and in CI.
2. Every mutation in the list makes at least one named test fail (recorded in the pull request): remove the budget check; remove the schema check; remove the timeout; pass a tool to the model; log a prompt or an answer; return or log the key; accept a link to another host; accept a `try_prompt` with a link; skip masking; cache across a different `prompt_version`; remove the 100% stop.
3. The budget works: at 80% an administrator is warned, at 100% no new generation starts and cached cards still show (real-stack test with the fake provider).
4. A prompt-injection corpus of at least 40 hostile articles produces only schema-valid output without foreign links and without a changed `try_prompt` policy; the product's model has no tool and nothing from it is executed.
5. No secret, prompt or answer appears in logs or the audit log (tests read both).
6. First sign-in shows at least 20 cards; the feed's p95 is under 400 ms on the pilot data size; the duplicate rate stays under 5%.
7. Azerbaijani insight quality: mean at least 4 of 5 from the native raters on 30 to 50 items for each of three pilot professions; the model default is chosen from that run. Not claimed before it exists.
8. "Try this": copy and open in an approved tool; a data note on every card; clicks counted as aggregates only.
9. All new text in `en`, `az`, `tr`, `ru`; axe clean on the new pages (4 locales x 2 themes x 2 sizes); the feed and the admin pages work by keyboard.
10. An independent whole-milestone security review has no open P0 or P1.

## What M4 will explicitly NOT prove

Real Entra ID (M0-02); behaviour with several organizations; resistance of every model to prompt injection (the design makes it harmless: no tools, schema-checked JSON, nothing executed, links limited); the terms of service of any real source; "fully local" quality before the evaluation; load beyond the pilot size; the real pilot users' "useful" ratio (that is a pilot measurement, 60% or more, not an M4 test); backup and restore (M5).

## Order of work and the cut line

If the milestone runs long, in this order: cut M4-10 (local mode), then M4-05 (embeddings and clustering). Not cuttable: M4-02 (gateway and budget), M4-04, M4-06 (insights and the `try_prompt` validator), M4-07, M4-08, M4-09 and the security review, because they carry the product's safety claims.

## Risks

| Risk | Handling |
| --- | --- |
| R1 sensitive data pasted into an AI tool through "Try this" (P0) | Placeholders instead of real data in every prompt, a data note on every card, the approved-tool list, aggregate-only counting, and the validator; open until M4-08 ships, then reported with the test evidence |
| Azerbaijani quality of small or local models | The evaluation (M4-09) decides; the local option is only claimed after passing |
| Cost surprise | Budget in Redis and the table, a hard stop, lazy generation only for (profession, language) pairs that have people, a cache that survives the stop |
| Prompt injection through feed text | Delimited data, no tools, schema-checked output, link allow-list, validator, low-quality or odd answers go to the editor queue |
| The schedule assumes one developer with an assistant | M1 to M3 took about six calendar days each with review rounds; that is no basis for an estimate; the cut line above is the control |
