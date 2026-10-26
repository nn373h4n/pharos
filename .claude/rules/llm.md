---
paths:
  - "backend/src/pharos/llm/**"
  - "backend/src/pharos/enrichment/**"
---

# LLM rules

- Call models only through `LLMGateway`; never import a provider SDK.
- The stage → model mapping comes from config (filter: small model; enrichment and insight: strong model).
- Prompts live in versioned files; bump `prompt_version` on any change.
- Wrap external text in clear delimiters and state that it is data, not instructions.
- No tools or function calling for the model. Validate output against a schema and fall back safely on invalid output.
- Cache insights by (item, profession, language, prompt_version); generate lazily; check the budget first.
- Company-internal content goes to a cloud model only when the org policy allows it, and only after masking.
- Tests use the deterministic fake provider.
