# Governance

Pharos starts with a **benevolent-dictator** model: the founder has the final say. As the community grows, this moves to a maintainer council. The change itself will be recorded as an ADR.

## Roles

| Role | Who | What they do |
| --- | --- | --- |
| Contributor | Anyone who sends a pull request, issue or translation | Follows [CONTRIBUTING.md](CONTRIBUTING.md) |
| Triager | A contributor invited by a maintainer | Labels and reproduces issues, closes duplicates; no merge rights |
| Maintainer | Listed in [MAINTAINERS.md](MAINTAINERS.md) | Reviews and merges, cuts releases, owns security reports |

A contributor with a steady record of good reviews and changes can be invited to triage, and later to maintain. Maintainers who are inactive for a long time are moved to emeritus status.

## Decisions

- Day-to-day decisions happen in issues and pull requests.
- Anything that changes architecture, the data model, a security boundary, dependencies or deployment is written as an ADR in [docs/adr/](docs/adr/) first. An ADR is `Proposed` until a maintainer accepts it; the founder decides when maintainers disagree.
- Product scope follows [docs/PLAN.md](docs/PLAN.md). Phase 2 and 3 features are not added to the MVP without an agreed priority and acceptance criteria.
- Security reports are handled privately under [SECURITY.md](SECURITY.md).

## Releases

Versions follow [Semantic Versioning](https://semver.org/). Release notes come from `CHANGELOG.md`. A release needs passing checks, a migration review and a rollback note.

## Changing this document

By pull request, with maintainer approval.
