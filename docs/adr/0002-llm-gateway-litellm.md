# 0002. LLM gateway on the LiteLLM SDK

- Status: Accepted
- Date: 2026-10-26

## Context

Each installation brings its own key (BYOK): the admin chooses provider, model and key, and the
project defaults to no paid provider (accepted decision 5). Tests, CI and the demo must run with
no external LLM. Ollama is the local option. LLM calls happen in three pipeline stages (cheap
filter, enrichment, insight) and must respect a per-organization budget. External content is
untrusted, so the model must never act: it receives delimited text and returns JSON that is
validated against a schema. Switching models must be a configuration change, not a code change.

## Options considered

1. **Direct provider SDKs** — pros: full access to provider features, no extra dependency.
   Cons: one code path per provider; BYOK across providers multiplies integration and test work;
   budget, retry and validation logic would be duplicated.
2. **LiteLLM Proxy as a separate service** — pros: central key management, a UI, rate limits
   out of the box. Cons: one more container, database and secret store for every self-hoster;
   another network hop and attack surface; no measured need in the MVP.
3. **Own provider abstraction** — pros: no third-party churn. Cons: we would reimplement and
   maintain many provider adapters that LiteLLM already provides.
4. **LiteLLM Python SDK behind an internal `LLMGateway`** — pros: many providers and Ollama
   through one in-process API; our policy lives in one module we own. Cons: LiteLLM changes
   quickly (risk R16), so versions must be pinned and integration-tested.

## Decision

Option 4. Application code never imports LiteLLM or a provider SDK; every call goes through
`pharos.llm.LLMGateway`, which performs, in order:

1. budget check (`llm_usage` + Redis counter; warn at 80%, stop new generation at 100% while
   cached insights keep being served);
2. stage -> model resolution from `llm_provider_configs.stage_model_map` (configuration only);
3. masking of organization content before any cloud call (organization content is not sent to a
   cloud LLM by default; when the admin enables it, masking applies and the change is audited);
4. timeout and retry with backoff;
5. JSON-schema validation of the response; invalid output is rejected, never repaired by eval;
6. telemetry without content (stage, provider, model, tokens, cost estimate).

The product LLM has no tools, function calling, shell or SQL access. A deterministic fake
provider is registered for tests, CI and demo mode. No LiteLLM Proxy service in the MVP.

## Consequences

- Positive: provider choice is pure configuration; one place to test budget, masking and
  validation; the default install needs no paid account.
- Negative: we depend on LiteLLM's release cadence; a Proxy may be reconsidered if many
  installations need central key management or cross-service rate limits.
- Follow-up: import-linter rule forbidding `litellm` imports outside `pharos.llm`; tests for
  budget cut-off, schema rejection, masking and "no content in telemetry/logs"; fake provider
  fixtures; pin LiteLLM in `uv.lock` with Dependabot.

## References

- `docs/ARCHITECTURE_PROPOSAL.md` accepted decision 5, sections 3, 6, 7, 11 (R16), 15
- [0005 Tenancy and RLS](0005-tenancy-and-rls.md), [0009 Output sanitization](0009-output-sanitization-nh3-csp.md)
