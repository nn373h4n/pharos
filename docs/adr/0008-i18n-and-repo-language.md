# 0008. Internationalization and repository language

- Status: Accepted
- Date: 2026-10-26

## Context

Pharos targets a global audience while the first deployment serves Azerbaijani-speaking users.
The UI ships in four languages (en, az, tr, ru). Contributors worldwide must be able to read the
code, commits, issues and docs. The planning documents were written in Azerbaijani and Turkish,
and older ones state that `az` is the default UI language.

## Options considered

1. **`az` as source and default language** — pros: matches the first deployment. Cons: most
   contributors and translators cannot work from an Azerbaijani source; other installations
   would fall back to a language their users may not read.
2. **`en` as source language and fallback, default locale chosen per installation** — pros:
   one common source for translators; every installation picks what its users need.
   Cons: Azerbaijani strings must be kept complete so the first deployment never shows fallbacks.
3. **Mixed repository language** (docs in the maintainer's language) — pros: faster for the
   maintainer. Cons: excludes most contributors; reviews and issues fragment.

## Decision

Option 2 (accepted decision 7).

- Frontend i18n uses `next-intl` with `frontend/messages/{en,az,tr,ru}.json`.
- `en` is the source language for keys and the fallback.
- The product default is `en`; each installation chooses its own default locale (the first
  deployment uses `az`).
- CI fails the build when any locale is missing a key.
- Insights are shown in the user's language with the original-language label and a link to the
  original.
- Everything committed to the repository is English: code, comments, commits, PRs, issues,
  README, ADRs and new docs.
- Existing planning documents (`docs/PLAN.md`, `PHAROS_MASTER_PROMPT.md`,
  `docs/ARCHITECTURE_PROPOSAL.md`) keep their original languages; an English version of the
  master prompt is prepared before G3. Where those older documents say "az is the default
  language", this ADR supersedes them.
- Claude Code talks to the maintainer in Azerbaijani; its repository output stays in English.

## Consequences

- Positive: one translation source; any organization can install with its own default locale.
- Negative: every user-visible string needs four entries in the same change; Azerbaijani
  quality depends on native-speaker review.
- Follow-up: missing-key check in CI and in the frontend skeleton (M1-07); Playwright smoke
  test per locale; per-installation default locale in organization settings; contributor note
  on translations in `CONTRIBUTING.md`.

## References

- `docs/ARCHITECTURE_PROPOSAL.md` accepted decision 7, sections 8, 13 (M1-07), 15
- `docs/PLAN.md` and `PHAROS_MASTER_PROMPT.md` (older "az default" statements, superseded here)
