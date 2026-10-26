# 0009. LLM output sanitization with nh3 and a strict CSP

- Status: Proposed
- Date: 2026-10-26

## Context

Insight cards display LLM output generated from untrusted external content. Prompt injection or
content poisoning can make the model emit HTML, scripts, tracking links or a "try this" prompt
that asks users for passwords or company data. The plan named `bleach` for sanitization, but
`bleach` is deprecated; `nh3` is the recommended replacement. React escaping alone does not cover
markdown rendering or links.

## Options considered

1. **Client-side DOMPurify only** — pros: well-known, runs where HTML is rendered. Cons:
   unsafe content is stored and served by the API; every client must remember to sanitize;
   API consumers and exports get raw output.
2. **`bleach` on the server** — pros: familiar Python API. Cons: deprecated; no long-term
   maintenance for a security-critical dependency.
3. **`nh3` on the server, plain text or restricted markdown, strict CSP, link filter,
   `try_prompt` validation** — pros: content is cleaned once before storage; defense in depth
   with CSP and React escaping. Cons: a native dependency; restricted markdown limits formatting.

## Decision

Option 3.

- LLM output fields are plain text or restricted markdown only; raw HTML is never allowed.
- The server sanitizes every field with `nh3` after JSON-schema validation
  ([0002](0002-llm-gateway-litellm.md)) and before writing `item_insights`.
- Link filter: the only URL allowed in output is the source item's own URL; all other links are
  removed.
- `try_prompt` validation: no links, no requests for passwords or personal data, a length limit,
  and placeholders instead of real data. Failures hide the item and send it to the editor queue.
- A strict Content Security Policy is served for the web app; React escaping stays on.

## Consequences

- Positive: stored insights are safe for every consumer; multiple independent layers against XSS.
- Negative: formatting options are limited; validator rules can reject valid output and need tuning.
- Follow-up tests: malicious HTML/markdown corpus, injection regression corpus, link-filter
  tests (foreign URLs, obfuscated URLs, markdown and autolinks), `try_prompt` rule tests, CSP
  header test.
- Follow-up docs: XSS and prompt-injection controls in `docs/THREAT_MODEL.md`. The `bleach`
  mention in `docs/PLAN.md` is superseded by this ADR.
- Adding `nh3` is a new dependency and needs the maintainer's approval when it is introduced.

## References

- `docs/ARCHITECTURE_PROPOSAL.md` sections 2 (bleach note), 6 (prompt injection), 7 (XSS), 11 (R4), 15
- `docs/PLAN.md` security section (original `bleach`/DOMPurify mention)
- [0006 Connectors](0006-connector-plugins-safehttpclient.md)
