# 0004. Background jobs with Celery and Redis

- Status: Accepted
- Date: 2026-10-26

## Context

Ingestion and all LLM calls run outside the request path: the feed API never calls an LLM, and
a missing insight is queued and shown as "being prepared". The pipeline has six stages, each an
idempotent task that may be re-run safely. We need scheduled fetching, retries with backoff,
separate queues so slow LLM stages do not starve ingestion, and a heartbeat for monitoring.
Redis is already in the stack for sessions, cache and rate limits. The master prompt requires the
Celery-or-arq choice to be justified in an ADR.

## Options considered

1. **Celery + Redis (worker + beat)** — pros: mature periodic scheduling (beat), retries,
   queue routing, wide community and operational knowledge. Cons: heavier, sync-first worker
   model; configuration surface is large; async code needs care inside tasks.
2. **arq** — pros: lighter, async-native, fits FastAPI style. Cons: smaller ecosystem and
   community; fewer ready answers for scheduling, routing and monitoring at scale.
3. **No queue (cron + in-process background tasks)** — pros: fewest moving parts. Cons: no
   retries or routing, work is lost on restart, LLM latency would compete with API requests.

## Decision

Option 1. `worker` and `beat` run from the same `pharos` package as `api` with a different
entry point (see [0001](0001-modular-monolith-platform.md)). Redis is the broker. Each pipeline
stage is its own idempotent task; tasks are routed to separate queues (for example ingestion
versus LLM stages) and use retries with backoff. Only `worker` and `beat` have egress, and only
to sources and LLM providers; Redis has none.

What would change this decision:

- operating Celery proves a real burden for self-hosters (for example recurring support issues
  with beat or worker configuration);
- the task code becomes predominantly async and Celery's model causes measured problems;
- arq's ecosystem gains the scheduling, routing and monitoring features we use.

Any switch needs a new ADR that supersedes this one.

## Consequences

- Positive: proven scheduling and retry semantics; queue isolation protects ingestion from LLM
  latency; one codebase for api and worker.
- Negative: Celery's size and configuration; idempotency is our responsibility, not the queue's.
- Follow-up: worker skeleton with beat and a heartbeat task that writes a timestamp to Redis and is
  shown in `/readyz` (M1-06; Prometheus metrics come with the `observability` profile);
  integration test against Redis; tests that re-running a stage does not change results;
  queue length and stage error metrics.

## References

- `docs/ARCHITECTURE_PROPOSAL.md` sections 4, 6, 9, 13 (M1-06), 15
- `PHAROS_MASTER_PROMPT.md` (Celery or arq to be justified by ADR)
- [0002 LLM gateway](0002-llm-gateway-litellm.md)
