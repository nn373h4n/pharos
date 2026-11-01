# 0018. What people do with the cards is private to them; the organization sees counts

- Status: Proposed
- Date: 2026-11-01

## Context

The Home feed (M4-07) lets people save a card, mark it useful or not useful, say they tried it, and
say how many minutes it saved. That is a record of what a person read and thought of their work.
The product exists to help employees, and employees must be able to trust that it is not a way to
watch them: no manager, administrator or the company that runs the installation should be able to
read what one person did. At the same time the organization needs to know whether the product
helps: how many people used it, how many cards were tried, how much time was saved (the pilot's
success measure).

The plan says interactions are counted "aggregate-only". A rule kept by care in the code is not
enough for a privacy promise: an administrator's role, a future endpoint or a mistaken query would
break it without anybody noticing.

## Options considered

1. Store the rows and filter in the API. — An administrator role, a new endpoint or a bug can read
   them; the promise rests on care.
2. Store no per-person rows, only counters. — Keeps nothing to leak, but a card cannot be shown as
   saved, a not-useful card cannot be hidden again, and a person cannot undo a click.
3. Per-person rows that the database shows only to the person, plus one function that returns
   counts. — The promise is enforced where the data is. Costs one policy and one function.
4. Differential privacy noise on the counts. — Stronger, but out of proportion for a pilot of a
   few dozen people and hard to explain to the people it protects.

## Decision

Option 3. Migration `0008`:

- `interactions(org_id, user_id, item_id, type, value, created_at)`, one row per (person, article,
  kind). The kinds are view, save, useful, not_useful, tried and time_saved (minutes, 1 to 480).
- A **restrictive row-level policy** (`user_id = pharos_current_user()`, for reading and writing)
  applies to every role except the table owner. The application names the person for each
  transaction (`app.user_id`). An administrator's request sees the rows of the administrator and of
  nobody else; a request that names nobody sees none.
- The organization reads **counts only** through `pharos_interaction_counts(days)`, a
  `SECURITY DEFINER` function with a fixed search path and a fixed time zone (UTC) that filters by
  the current organization and returns, per **complete UTC day** and kind, how many people and
  actions there were and the minutes saved. A day and kind that rests on **fewer than five people
  is left out**. Each person's minutes count for at most 120 in a day's total and the total is
  rounded to a multiple of 30, so one figure does not give away one person's. The caller passes
  only a number of days, clamped to 1 to 90; it cannot choose the window's edge or the day's
  boundary. Execution is granted to the application role only, not to `PUBLIC`.
- A row never moves to another day: `created_at` is not changed by a repeated click, and minutes
  may be corrected only on the day they were given. Without this, successive answers could be
  subtracted from each other to see one action.
- The feed hides a card the person marked not useful and marks one they saved; both are read from
  the person's own rows.

## Consequences

- No screen and no endpoint can show who did what; the only way to read a row is as that person.
- Found by the security review and closed in the design: a live, moving window and a caller-chosen
  `since` or time zone let an administrator difference two answers and recover single actions and
  minutes; that is why only complete days, a clamped `days`, a fixed time zone and rows that stay on
  their day are part of the decision. Still open (F-34): when a person takes back an action
  (`DELETE`) the count of a completed day drops, and an observer who polls could notice the time;
  the remedy is a nightly snapshot of completed days, written once.
- A very small team (under about eight people) cannot get meaningful anonymity from counts, and
  a person who knows the other four of five can subtract them. The pilot should be told so, and the
  floor can be raised in the function by a new migration.
- Whoever owns the database (the operator) can read the table; that is a property of self-hosting
  and is stated in the threat model.
- Tests pin it: another person's rows are invisible and unwritable at the database level, the
  summary hides small groups and sums minutes for groups of five or more, an administrator's answer
  holds no person or article identifier.

## References

ADR-0012 (the personal-rows policy this reuses), ADR-0015 (the cache and the card), `docs/plans/m4-llm-layer-and-personal-feed.md` (M4-07); migration `0008_interactions`.
