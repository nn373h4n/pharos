---
name: adr
description: Creates a new Architecture Decision Record in docs/adr/. Use whenever a decision changes architecture, data model, security boundary, dependencies or deployment.
argument-hint: "[short-title]"
---

1. Find the next number: highest `docs/adr/NNNN-*.md` plus one, zero-padded to four digits.
2. Create `docs/adr/NNNN-<kebab-title>.md` from the template below. Status is `Proposed` unless the user accepted the decision in this conversation.
3. If it replaces an ADR, link both ways and mark the old one `Superseded by NNNN`.
4. Tell the user the decision in two sentences and ask for acceptance.

Template:

```markdown
# NNNN. <Title>

- Status: Proposed | Accepted | Superseded by NNNN
- Date: YYYY-MM-DD

## Context
The problem, constraints and forces.

## Options considered
1. <Option> — pros / cons
2. <Option> — pros / cons

## Decision
What we chose and why.

## Consequences
Positive, negative, and follow-up work (tests, docs, migrations).

## References
Issues, PRs, docs.
```
