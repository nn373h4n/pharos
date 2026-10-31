# 0016. LLM budget and cost accounting

- Status: Proposed
- Date: 2026-10-31

## Context

Model calls cost money and a bug, a loop or a hostile feed could spend a month's budget in an hour. ADR-0002 says the gateway checks a budget first, warns at 80% and stops new generation at 100% while the cache keeps being served. The maintainer decided on 2026-10-31 that the default cap is small and the administrator raises it after the pilot shows the real cost. The estimate in the proposal (600 insight calls a day) is an assumption nobody has measured.

## Options considered

1. **Count after the call.** Pros: exact. Cons: many parallel calls overshoot before any count lands.
2. **Reserve an estimate before the call, settle after.** Pros: parallel calls cannot overshoot by more than the estimate error. Cons: needs an atomic step and an estimate.
3. **Rely on the provider's own spending limit.** Cons: not portable (a local model, other providers), and it fails after the money is spent.

## Decision

Option 2, kept in Redis and written to the database.

- **Unit:** the cost is counted in micro-US-dollars, an integer. The estimate of a call is `(prompt tokens + the stage's maximum answer tokens)` at the model's price from the pinned price table of the gateway; a model without a known price is charged at the price in the settings (`PHAROS_LLM_PRICE_IN_MICRO_USD`, `PHAROS_LLM_PRICE_OUT_MICRO_USD`, 20 and 100 US dollars per million tokens by default) so it can never look free; an operator raises them for a dearer model, otherwise the cap is reached later than it should be.
- **Reserve and settle:** before a call the gateway reserves the estimate against the day's counter in one atomic Redis step (a script, the way the rate limiter works). After the call it settles to the real cost from the reported tokens. A failure that cannot have been billed (the provider answered with an error status, nothing was sent) releases the reservation and a retry reserves again; one that may have been billed (a timeout, a broken connection, a cancelled task) **keeps the reservation at the estimate**, so slow or missing answers cannot spend more than the cap plus one estimate. If the settling itself fails after a paid call, the answer is kept, the usage is recorded, and the next call stops because the counter cannot be read. If the reservation would pass the cap, the call is refused and nothing is sent.
- **Warn and stop:** at 80% of the cap a warning is recorded once a day (audit event `llm.budget_warning`, a banner on the admin page); at 100% new generation stops (`llm.budget_exhausted`) while cached insights and cards keep being served. The cap is per day per installation, set by an administrator on the admin page; the default is small. Changing the cap is audited.
- **Fail closed:** if Redis does not answer, no call is made. A missing or unreadable budget is the same as an exhausted one.
- **Record:** every call writes a row to `llm_usage` (organization, stage, provider, model, tokens in and out, estimated cost, outcome, time) in the same step that settles the counter. Never the prompt, the answer, the item or the person. Reports group by stage, model and day.
- **Limits that do not depend on money:** a maximum number of calls per minute per installation, a maximum answer length per stage, a timeout, and a bounded retry (counted in the budget). A stage that keeps failing is paused for a while instead of retried in a loop.
- **Where it runs:** in the worker. The counters live under the worker's Redis namespace (`pharos:worker:llm:*`, the keys the worker's Redis user may use); the API reads them for the admin page through the database rows and a read of the same keys it is already allowed to use for that purpose, and never writes them.

## Consequences

- Positive: a hostile feed or a bug cannot spend more than the cap plus one estimate error; the cost report needs no content; the stop is testable with the fake provider.
- Negative: a conservative estimate stops generation a little early; the price table must be kept current with the pinned LiteLLM version; one more Redis script.
- Follow-up: tests for the atomic reserve under parallel calls, settle and release, the 80% and 100% thresholds, fail-closed on Redis loss, no content in `llm_usage`, audit events; mutation checks for each (remove the reserve, the stop, the fail-closed, the content-free record).

## References

- ADR-0002, ADR-0013 (the pattern for results through Redis), ADR-0015; `docs/ARCHITECTURE_PROPOSAL.md` section 6 (cost model); `docs/plans/m4-llm-layer-and-personal-feed.md`
