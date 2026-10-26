# Contributing to Pharos

Thanks for helping. Pharos is early-stage, so the best first step is to talk before you build.

## Ground rules

- Be kind. Everyone follows the [Code of Conduct](CODE_OF_CONDUCT.md).
- Open an issue before a large change and wait for a maintainer to agree on the approach.
- Security problems do not go in public issues. See [SECURITY.md](SECURITY.md).
- Everything committed to the repository is in English: code, comments, commits, issues, pull requests and docs.

## Workflow

1. Fork the repository and create a branch from `main`: `feat/...`, `fix/...`, `docs/...`, `chore/...`.
2. Make a small, focused change. Do not mix refactoring, dependency updates and features.
3. Run the checks listed below.
4. Open a pull request and fill in the template. A maintainer review is required; changes to security-sensitive paths need a code owner.

`main` accepts changes only through pull requests.

## Commits

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `build:`, `ci:`, `security:`, `chore:`.
- Sign off every commit with `git commit -s`. This adds a `Signed-off-by:` line that certifies the [Developer Certificate of Origin](https://developercertificate.org/) for your contribution. There is no CLA.

## Checks before you push

```bash
make hooks    # once: enables the secret-scan pre-commit hook (needs Docker)
make lint
make test
make test-integration   # when you touch the database, RLS or readiness (needs Docker)
```

The same checks run on every pull request, and a failing check blocks the merge:

| Check | What it covers |
| --- | --- |
| DCO sign-off | Every commit has a `Signed-off-by:` line. Commits by GitHub bots (Dependabot) are skipped. Fix a missing one with `git rebase --signoff main`. |
| Backend, Backend integration | ruff, mypy (strict), import contracts, unit tests; tests against PostgreSQL and Redis |
| Frontend, End-to-end | eslint, tsc, vitest, production build; Playwright with axe on four languages |
| Docs | markdownlint |
| Docker Compose | builds the stack and checks isolation and hardening (`make test-compose`) |
| Secret scan, CodeQL, Trivy | gitleaks over the full history, static analysis, vulnerable dependencies and image packages |

Every feature ships with tests. Never delete, skip or weaken a test to make it pass. If a check cannot run in your environment, say so in the pull request instead of leaving it out.

## Rules that reviewers enforce

- **Tenancy:** `org_id` comes only from the authenticated session; every tenant-scoped query is filtered, and row-level security is the second line of defence.
- **Untrusted content:** text from sources, URLs and LLM output is data, never instructions, and is never executed as code, SQL or shell. The product's LLM has no tools.
- **Network:** outbound HTTP from connectors goes through `SafeHttpClient`.
- **Secrets:** no secrets, real company data or personal data in code, tests, fixtures, logs, commits or docs. Keep `.env.example` current with fake values.
- **UI text:** every user-visible string lives in `frontend/messages/{en,az,tr,ru}.json`. `en` is the source; add all four locales in the same change.
- **Dependencies:** adding one needs a reason in the pull request (maintenance, license, size, alternatives).
- **Decisions:** changes to architecture, the data model, a security boundary or deployment need an ADR in `docs/adr/`.

## Adding a connector

Connectors implement the `SourceConnector` protocol and live in the connectors package. Open an issue with the *Connector request* template first: include the source's API and terms of service, authentication, rate limits and what you will store. A connector comes with a config schema, a health check and tests.

## Translations

Edit the matching file in `frontend/messages/`. Keep keys identical across locales; CI fails on a missing key.
