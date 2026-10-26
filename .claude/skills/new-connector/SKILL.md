---
name: new-connector
description: Plans and builds a new content source connector that implements SourceConnector.
argument-hint: "[source-name]"
disable-model-invocation: true
---

Build a connector for: $ARGUMENTS

Phase 1 — plan, then stop for approval:
1. Read the `SourceConnector` protocol, `SafeHttpClient` and one existing connector.
2. Research the source: official API vs RSS, authentication, rate limits, pagination, ToS and robots.txt. Prefer the officially supported, free access path.
3. Present: mapping to `RawItem`, config schema (no secrets in defaults), timeout / retry / rate-limit policy, dedup key, risks, and a test matrix: happy path, empty result, pagination, 429 and 5xx, malformed payload, oversized response, redirect to a private IP.

Phase 2 — build, after approval:
4. Implement in `connectors/<name>/` with config schema, `fetch`, `health_check` and entry-point registration.
5. Tests use recorded fixtures — no live network calls.
6. Store title, short excerpt and link only; keep the original URL.
7. Update `docs/CONNECTORS.md`, then run `definition-of-done`.
