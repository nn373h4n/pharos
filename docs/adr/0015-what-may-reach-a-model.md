# 0015. What may reach a model, how the answer is limited, and how it is cached

- Status: Proposed
- Date: 2026-10-31

## Context

ADR-0002 puts every model call behind `LLMGateway` and says organization content is not sent to a cloud model by default. M4 turns that into rules a test can check. Three things need settling before any code:

- **Which data may go to a model.** The maintainer decided on 2026-10-31 that only public article text goes to a cloud model, to Anthropic first.
- **The content plane.** ADR-0005 assumes `scope = public` content with a shared cache keyed by (item, profession, language, prompt version). M3 built `items` per organization (and per person for personal feeds) with no `scope` column. One installation serves one organization (ADR-0005), so the two can be reconciled without a shared plane.
- **How far a model's answer is trusted.** An article is written by a stranger and can say anything, including instructions to the model. The product's model has no tools, but its answer is shown to people and contains a prompt they may paste into another AI tool.

## Options considered

1. **Send whatever helps and mask afterwards.** Cons: a missed pattern is a leak; the rule is untestable.
2. **Send only a fixed list of public fields, enforced by types and tests.** Pros: the boundary is small and checkable. Cons: no company-specific cards in M4.
3. **A shared public content plane across installations.** Cons: a project of its own (federation, trust, takedown); not needed for one organization per installation.

## Decision

Option 2.

**1. Three classes of data.**

- **Class A, public article text:** the title, the excerpt (at most 500 characters), the host of the article's link and its language, of an organization item (`owner_user_id IS NULL`, status `new`). Only class A may be sent to a cloud model.
- **Class B, organization and person content:** anything a person typed (feedback text, personal prompts), onboarding answers, names, e-mail addresses, departments, audit data, source configuration. Never sent to a model in M4.
- **Class C, personal feed articles** (`owner_user_id` set): never sent to a model, never enriched, on behalf of anybody. They stay plain items shown to their owner.

**2. The boundary is in types and in one query, not in care.** The gateway accepts only a `PublicArticleText`. The only product code that makes one is `pharos.llm.repository.load_public_article`, which reads the article from the database in the organization of the call and only if it has no owner and its status is `new` (the caller hands over an identifier, never a row; a test fails if any other product code calls the function that mints it); the marker that proves it is not a constructor argument, so `dataclasses.replace` and a hand-built object are refused. A stage cannot pass a free string either: a caller names a **task** of the product (an enum) and typed parameters (a language code, a profession code from the catalogue); the instruction of each task is a versioned file written by us (`prompts/<task>.<version>.txt`), and the schema of the answer is added to the prompt by the product from a closed table of classes (`pharos.llm.schemas`, one per task); the gateway refuses any other class, because a schema carries text (descriptions, enum values) into the instruction slot, and requires every object in it to forbid extra fields. The whole message (rules, task text, schema, layout) is pinned by a test, so a change needs a new prompt version. Tests prove a personal item, a held item and a hidden item never reach the gateway, and that the gateway refuses anything that is not that type.

**3. Masking stays as defence in depth.** The gateway also masks e-mail addresses, phone numbers, IBANs and national-identity patterns in every outgoing text, public or not, so a mistake in class A does not publish a stranger's address to a vendor. Masking is counted in telemetry (how many, never what). Company content is not sent in M4; if a later milestone allows it, it needs its own ADR and an audited switch.

**4. What a model's answer may contain.** Every answer is parsed as JSON against a schema (ADR-0002); anything else is rejected and never repaired. Text fields are plain text of a bounded length. A link is kept only if its host is the host of the item's own canonical link; other links are removed. The `try_prompt` passes a validator: no link, no request for a password, key or personal data, a length limit, and placeholders such as `[TEXT OF THE DOCUMENT]` instead of real data. A failure, a very low quality score or a suspicious answer holds the item for the editor queue (`held`, reason `low_quality`) and is never shown. Answers are rendered as text only (ADR-0009).

**5. Prompt injection is made harmless, not detected.** The article is given as delimited data; the system prompt says it is data and not instructions; the model has no tools, no function calling, no network, no database and no shell; nothing in the answer is executed, evaluated or used to build SQL, a path or a URL. A test corpus of hostile articles (instructions to ignore rules, to output links, to reveal the prompt, to emit markup, to change the output's structure) must produce only schema-valid, link-limited, text-only output.

**6. The cache key.** `item_insights` is unique on (item, profession, language, prompt version). The person's AI level and interests influence ranking and the tone of the card shown, not the key, to keep the number of calls at the number of (profession, language) pairs that have people. There is no tenant in the key because an installation has one organization and the item already belongs to it; ADR-0005's public plane stays a later step. A new prompt version does not rewrite old insights in bulk; they are refreshed when viewed.

**7. The provider key.** It reaches the worker's environment only (the API and the scheduler never hold it; in production the API refuses to start with one). In M4-03 it is stored encrypted with a key derived from `PHAROS_SECRET_KEY` for its own purpose (`KeyPurpose.LLM_KEYS`), with a `key_version` for rotation. It is never returned by the API, never logged, never sent to the browser; the admin page shows only whether one is set and when it was last tested. A wrong key fails closed (no calls, a clear admin message).

**8. Where the calls run.** In the worker, outside the request path (ADR-0004). "Test the key" is a worker task with its result read through Redis, the pattern of ADR-0013.

**9. A request that may have been billed is counted.** A timeout, a broken connection or a cancelled task keeps its booking at the estimate; only a refusal before billing (an error status, or nothing sent) gives it back. The cap therefore holds even when answers go missing (ADR-0016).

**10. The provider's address is an operator decision.** LiteLLM makes its own HTTPS calls to the provider's endpoint, so those calls do not pass `SafeHttpClient`. The contract that keeps `httpx` out of the application looks at the application's own imports and does not see inside LiteLLM, so a new contract instead confines `litellm` to the one module `pharos.llm.litellm_provider`. The risk is smaller than for feeds because the endpoint is chosen by an administrator, not by article text: the gateway accepts only a configured provider, with an `https` base URL whose host does not resolve to a private address (except in the local-model profile, where the operator names the host), and no article content ever decides the address.

## Consequences

- Positive: the rule "only public text goes out" is a type and a test, not a habit; one place to read what leaves the installation; the cost scales with (profession, language) pairs.
- Negative: no cards about the organization's own content in M4; the one module that imports LiteLLM must stay small and reviewed; masking can alter a legitimate public text (an author's address) and is accepted.
- Follow-up: import-linter contracts (only `pharos.llm.litellm_provider` imports `litellm`; the API never imports a provider); tests for class A/B/C, masking, schema rejection, link filtering, the validator, the hostile corpus, key handling (never in a response, a log or the audit log); `docs/DATA_MODEL.md` and `docs/THREAT_MODEL.md` updated with the first migration.

## References

- ADR-0002, ADR-0004, ADR-0005, ADR-0009, ADR-0013; `docs/ARCHITECTURE_PROPOSAL.md` section 6; `docs/plans/m4-llm-layer-and-personal-feed.md`
