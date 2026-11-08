# 0027. The Home feed ranks once in five minutes for everyone who shares a profession

- Status: Proposed
- Date: 2026-11-08

## Context

The load test of M5-08 (`make loadtest`, F-49) found that one feed request costs time in
proportion to the number of articles in its 120-day window. The query scores every eligible
article (about 20,000 in the test data) with a formula that mixes the fit to the person's
profession, recency, quality and two category bonuses, and only then keeps the best twenty. Alone
it takes about 390 ms and 120,000 buffer reads; forcing hash joins did not help (373 ms), so it is
not a planner mistake. With 50 people at work, one visit every 30 seconds, the stack on a machine
with two CPU threads answered the first page with p95 6.3 s; with 150 people it began to fail (5%
of requests) and with 300 people 44% of requests failed. The target of decision D5 is p95 under
400 ms with 50 people at work, and the cost must not grow with the corpus.

The score has two kinds of terms:

- **Shared by everyone with the same profession at the same moment:** the fit of the article to the
  profession, its recency, its quality (together at most 0.85 of the score), and which article of a
  cluster of copies stands for the story (ADR-0021).
- **The person's own:** the two category bonuses (0.10 for the person's interests, 0.05 for their
  level, so at most 0.15), and what they marked not useful (hidden) or saved. The interests and
  levels of 50 to 100 people are nearly all different, so a cache keyed by them would hardly ever be
  reused; only the bonuses and the hidden articles are the person's, and they are cheap.

The cursor already carries the moment the feed was ranked at (`as_of`), so the pages of one visit
are ranked at one moment.

## Options considered

1. **Rank once in five minutes for each profession and keep the best two thousand in Redis, then add the
   person's own terms in the application.** Per request: read the list, add the bonuses, take the
   slice after the cursor, read the cards of that slice from the database (checking again that each
   article may still be shown), drop what this person hid. Pros: the cost of a request stops
   depending on the corpus; no migration; the person's own data never enters the shared list; the
   number of cached lists is the number of professions, not of profiles. Cons: a new article or a
   change of ranking shows up to five minutes late; a cache in Redis, whose memory is fixed and which
   never drops keys; the exactness of the cut list needs an argument and a test; a person who pages
   past the exact part falls back to the slow query.
2. **The same list, keyed by profession, interests and level.** Pros: simpler arithmetic. Cons: the
   keys are almost one for each person, so the saving is lost; rejected.
3. **A candidate table kept by the worker for each profession.** Pros: one source of truth in
   PostgreSQL. Cons: a migration, a job that must keep up with every change of an article, and the
   per-person terms still have to be added on each request.
4. **A shorter window** (for example 30 days instead of 120). Pros: trivial. Cons: it only moves the
   point where the cost explodes, and it changes what people see.
5. **Store the head of each cluster as a flag and tune the query.** Pros: removes the correlated
   sub-query, part of the cost. Cons: the cost is still in proportion to the corpus; may follow
   option 1 if the ranking itself becomes too slow.

## Decision

Option 1.

- **When it is ranked.** A first page is ranked at the start of the current five minutes; the
  cursor carries that moment, as it does now. Everyone who asks in the same period, in the same
  organization, with the same profession, shares one ranking. Five minutes, not one: with a minute
  the load test showed a slow answer for the first request of each profession every minute (p95
  798 ms at 50 people); with five it did not (p95 134 ms), and new articles come hourly at the
  fastest and are read by the model a minute or more later.
- **Only moments the feed chose.** Cursors are not signed, so a cursor that names a moment that is
  not the start of a period is refused, and a ranking is made only for the current and the previous
  period; a cursor from an older one reads what is still kept and otherwise gets the live query.
  Without this a member could make a new 170 KB ranking with every forged second, and fill the
  Redis that also holds the sessions (found by the security review, fixed before merge).
- **What is kept.** For each (organization, profession, five minutes), the best 2,000 articles by the
  shared part of the score, each with its category and the exact decimal the database worked out,
  in Redis under `pharos:api:feed:v1:` for fifteen minutes. Nothing about a person is in it: no
  interaction, no identity, no text of an article.
- **How the person's part is added.** The application adds the two bonuses to the shared part with
  exact decimal arithmetic and floors once, which is what the live query does in SQL; a test
  compares the two page by page, cursor by cursor.
- **How far the list is exact.** An article that is not in the list has a shared part no higher than
  the last one in it, so its whole score is at most that plus 0.15. Only scores above that line are
  certain to be in the list and in order. When a page needs more than the certain part, the existing
  query goes on from the last article looked at, at the same moment, so the order does not change.
  A list that holds every article there is (fewer than 2,000) is certain all the way.
- **How a page is made.** The ids after the cursor are taken from the ordered list; one query reads
  their cards for the organization and leaves out every article that is no longer public, current,
  `new` and read, so a hold, a hide or a deletion by an editor takes effect at once; it marks what
  this person saved and hid; hidden ones are skipped and the next ids are read until the page is
  full.
- **How it is made once.** One request ranks while the others wait (a lock that only its owner
  lets go, up to one second); one that is still without a list then answers its own page with
  the live query. It does not rank the whole list again: that would add the most work when the
  database is busiest.
- **Redis down or slow.** The feed answers from the existing query, correct and slow. The cache has
  its own client with a one-second timeout: it must not make the feed depend on a second service.
- **What stays as it is.** The formula, the weights, the cluster rule, the cursor format, the page
  size and the privacy of interactions.

## Consequences

- Positive: a feed request costs a Redis read and two small queries whatever the corpus; the ranking
  is paid once for each profession and five minutes (about 0.4 s now).
- Negative: a new article, or a change of which copy stands for a story, can take up to five
  minutes to show, and a re-read article can show twice for that long (once from the kept list,
  once from the live query where they hand over); a removed one never stays. When an editor holds the copy that stood for a story, the story
  is gone from the feed until the next ranking, then shows with another copy. The ranking
  of each profession is made every period in which someone asks, even if it is one person.
- Redis: the list of one profession is about 170 KB. Redis has 200 MB and never drops keys, and the
  sessions live there too. At most the current and the previous period are made, each kept for
  fifteen minutes, so normally three and at most five lists live for each profession: tens of professions are a few
  megabytes, hundreds of professions could reach tens of megabytes (noted in the threat model).
- Follow-up: `make loadtest` is the acceptance (feed p95 under 400 ms with 50 people, and the
  stepped run shows where it stops). Option 5 can follow if the ranking itself becomes too slow.

## References

- F-49 and M5-08 in `TASKS.md`; `docs/DEPLOYMENT.md` ("Load test"); ADR-0018 (interactions are
  private), ADR-0021 (one card for one story); `backend/src/pharos/cards/ranking_cache.py`.
