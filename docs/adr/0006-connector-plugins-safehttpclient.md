# 0006. Connector plugins and mandatory SafeHttpClient

- Status: Proposed
- Date: 2026-10-26

## Context

Content comes from RSS, GitHub, Hacker News and Reddit in the MVP; arXiv and YouTube are Phase 2.
The community should add connectors without touching core code. Admins and users can add their
own feed URLs, which makes server-side request forgery (SSRF) into the installing organization's
internal network a real risk (R8). Source terms of service and copyright limit what we may store.

## Options considered

1. **Connectors inside the backend package** — pros: simplest packaging. Cons: every new
   connector is a core change and core review; no clear SDK boundary for contributors.
2. **Separate package discovered via Python entry points, with a small SDK** — pros: community
   connectors install like any package; core depends only on a protocol. Cons: plugins run
   in-process, so the network boundary must be enforced by our client and by the network.
3. **Out-of-process plugins (separate service or sandbox)** — pros: strongest isolation.
   Cons: a new service for every self-hoster with no measured need.

## Decision

Option 2.

- Connectors live in a separate package (`pharos-connectors`) in the `uv` workspace. The
  `SourceConnector` protocol lives in a small SDK package. Connectors are discovered through
  the Python entry point group `pharos.connectors`.
- A connector implements `fetch(since)` and writes only to `raw_items`; normalization and
  dedup belong to the pipeline.
- All outbound HTTP goes only through `SafeHttpClient`:
  - scheme allowlist (`http`, `https`);
  - DNS resolution first, then block private, loopback, link-local and cloud metadata addresses;
  - the same checks on every redirect hop;
  - connect/read timeout and response size limit.
- Default-deny egress at the network level: only `worker`/`beat` reach sources, PostgreSQL and
  Redis have no internet access. `SafeHttpClient` and the network policy are independent layers.
- X/Twitter is off by default (paid, restricted API). Reddit's API terms need legal review;
  subreddit RSS is the alternative.
- From external content we store title, short excerpt and link only, never the full text; the
  UI always links to the original.

## Consequences

- Positive: contributors add connectors without core changes; one audited HTTP path.
- Negative: in-process plugins are trusted code; a malicious package could bypass the client,
  so installing third-party connectors is an admin decision and network egress remains the backstop.
- Follow-up tests: SSRF suite (`169.254.169.254`, DNS rebinding, redirect chains to private
  addresses, oversized responses, slow responses); a lint or import-linter rule forbidding
  direct `httpx`/`requests` use in connectors; compose smoke test that postgres and redis cannot
  reach the internet.
- Follow-up docs: `new-connector` skill and contributor guide; egress allowlist in deployment docs.

## References

- `docs/ARCHITECTURE_PROPOSAL.md` sections 2, 3, 6 (Sources), 7 (SSRF), 9, 10, 11 (R7, R8), 15
- [0001 Modular monolith](0001-modular-monolith-platform.md), [0004 Background jobs](0004-background-jobs-celery.md)
