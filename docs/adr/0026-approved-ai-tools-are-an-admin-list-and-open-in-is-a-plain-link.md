# 0026. Approved AI tools are a short list kept by an administrator, and "Open in" is a plain link

- Status: Proposed
- Date: 2026-11-05

## Context

The "Try this" panel of a card gives a person a prompt to copy. The product plan wants the card to
help a person go on from there, but only to AI tools that the organization has approved: the main
risk of the product (R1) is people pasting confidential data into an AI tool, and a card that sends
people to an unknown tool makes it worse. Until now the card offered "Copy" only, and the list of
approved tools was an open item (F-35, decision D5 of the M4 plan: an admin-edited list, empty by
default).

Three things shape the design. The server must never be made to reach an address an administrator
types (that would be a new way to server-side request forgery), so the address is opened by the
person's browser and not by Pharos. The prompt came from a model and is about a stranger's
article; putting it into an address would copy it into the tool's server logs and the browser
history. And an address is a link people click: it must not hide where it goes.

## Options considered

1. **A list kept by an administrator, a plain link next to "Copy" (chosen)** — pros: nothing
   reaches the server of the tool unless a person pastes it; the list is short and auditable; the
   default (empty) keeps today's behaviour. Cons: one more click and a paste for the person.
2. **A link that carries the prompt in the address** (`?q=...`) — pros: one click. Cons: the prompt
   goes into logs and history outside the organization's control; tools differ in what they accept;
   long prompts break the address. Rejected.
3. **A list in the settings file or the environment** — pros: no table. Cons: a change needs a
   restart and an operator, not an administrator; no audit trail of who changed it.
4. **Any address, no list** — pros: nothing to build. Cons: removes the point of "approved".

## Decision

Option 1.

- A table `ai_tools(id, org_id, name, url, created_by, created_at, updated_at)`, migration 0010,
  with the usual row-level policy on `org_id`. At most **ten** tools per organization, enforced by
  the service under an advisory lock and by a trigger. A name is unique per organization, whatever
  the case or Unicode form. Only the application role touches the table (it may change the name,
  the address and the time of the change); the worker has no privilege on it.
- `GET /api/ai-tools` for everybody who is signed in (name and address, nothing else);
  `POST`, `PATCH`, `DELETE` for administrators only, with the usual CSRF and origin checks. Each
  change is audited (`ai_tool.created`, `ai_tool.updated`, `ai_tool.deleted`); an audit entry
  names the tool by its identifier and never holds the name or the address. A refused duplicate or
  a refusal at the cap leaves a failure entry.
- An **address** is accepted only if a browser may safely be sent to it: `https`, at most 300
  characters, no credentials, no fragment, no white space, control, zero-width, bidirectional or
  other invisible character, an ASCII host name (an international name is written in its punycode
  form), and **not a bare IP address** in any spelling (a bracket, or a last label that is a
  number, which is how a browser reads an address). `localhost` and every `*.localhost` are
  refused: a browser sends them to the person's own machine. An internal host name is allowed on
  purpose: an organization may run its own AI portal. The checks are made in the service, repeated
  by the table as a second line, and repeated again in the browser before a link is made. A name is
  plain text; markup or invisible characters are refused, not repaired.
- A card shows, next to "Copy", a link "Open in {tool}" for each tool. It opens a **new tab**,
  sends **no referrer** (`rel="noopener noreferrer"`) and carries **no part of the prompt, the
  article or the title**. The person copies the prompt first and pastes it there; a note on the
  card says so. The data note ("do not paste confidential data") stays on every card. With no tool
  in the list, or if the list cannot be read, the card offers "Copy" only.
- Which tools are approved is the maintainer's decision at the pilot (gate G9) and is entered on the
  administration page; no tool name or address is kept in the repository.

## Consequences

- Positive: the product can point people to the tools the organization chose, without sending the
  prompt anywhere; an administrator can change the list without an operator; every change leaves a
  trace.
- Negative: a change reaches other people's cards after at most a minute (the page keeps the
  list for a minute) and not at all in a suggestion that is already open, so deleting a hostile
  tool is not instant; the link shows the host it goes to next to the name, but the name is the
  administrator's choice and look-alike letters are not folded by the uniqueness rule; an
  administrator who is compromised can add a link to a hostile site (it is audited and
  limited to ten, but a person who clicks it is on that site); the product cannot stop a person from
  pasting confidential text into an approved tool, only remind them (R1 is reduced, not removed).
  Clicks on "Open in" are not counted (a new interaction type would change ADR-0018); follow-up.
- Follow-up: a count of "Open in" clicks as an aggregate, if wanted; per-profession lists, if the
  pilot shows a need.

## References

- F-35 and M5-07 in `TASKS.md`; ADR-0015 (what may reach a model), ADR-0018 (interactions),
  ADR-0019 (the strict CSP); `docs/plans/m5-pilot-readiness.md` (M5-07, D9, gate G9).
