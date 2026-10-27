# Pharos

An open-source, self-hosted platform that shows employees practical, profession-specific AI use cases in their own language.

Most people hear that "AI will change everything" but not where it helps *their* job. Pharos collects real AI use cases from public sources, filters them by profession, and explains each one in the reader's language: what it is, how it applies to their work, and a ready-to-use prompt to try.

> **Status: early development (pre-alpha).** The repository currently contains the project plan, architecture decisions and tooling. The application is built milestone by milestone and does not run yet. See [Roadmap](#roadmap).

## What it will do

- Collect AI use cases from RSS, GitHub, Hacker News and Reddit through pluggable connectors, then normalize and de-duplicate them.
- Rank content by the reader's profession, interests and AI level.
- Show each item as a short card: a two-sentence summary, "how this applies to your job", and a "Try this" prompt.
- Work in English, Azerbaijani, Turkish and Russian; every installation picks its default language.
- Let each organization bring its own LLM provider and key (OpenAI, Anthropic, Gemini or a local Ollama model) behind a budget limit.
- Sign users in with any OpenID Connect provider (Entra ID, Google Workspace, Okta, Keycloak) or local accounts.

One installation serves one organization and runs on a single Linux VM with Docker Compose. External content is treated as untrusted data, and the product's LLM has no tools: text goes in, schema-validated JSON comes out.

## Quick start

You need Docker with Compose and `make`. Three commands start the whole stack:

```bash
git clone https://github.com/nn373h4n/pharos.git && cd pharos
make env   # creates deploy/.env with generated passwords
make dev   # builds the images and starts everything
```

Then open `https://localhost` (your browser will warn about the locally signed certificate). The
stack currently serves the placeholder home page in four languages and the health endpoints; sign-in,
sample content and the demo LLM provider arrive with the next milestones. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for configuration, TLS, ports and known limits.

## Develop

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js with pnpm, Docker with Compose, and `make`.

```bash
uv sync                                          # backend dependencies
pnpm --dir frontend install                      # frontend dependencies
pnpm --dir frontend exec playwright install chromium   # browser for the end-to-end tests

make help          # list targets
make hooks         # enable the secret-scan pre-commit hook (needs Docker)
make secrets-scan  # scan the git history for secrets
make lint          # ruff, mypy, import contracts, eslint, tsc
make test          # hook tests, backend and frontend unit tests
make test-e2e      # Playwright against the production build (4 languages, axe checks)
make test-integration  # PostgreSQL and Redis in throwaway containers (needs Docker)
make test-compose  # builds the Compose stack in a throwaway project and checks it (minutes)
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Commits need a DCO sign-off (`git commit -s`).

## Documentation

| Topic | Where |
| --- | --- |
| Product plan | [docs/PLAN.md](docs/PLAN.md) |
| Architecture | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Data model | [docs/DATA_MODEL.md](docs/DATA_MODEL.md) |
| Threat model | [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) |
| Decisions | [docs/adr/](docs/adr/) |
| Governance | [GOVERNANCE.md](GOVERNANCE.md) |
| Security policy | [SECURITY.md](SECURITY.md) |

`docs/PLAN.md` and `docs/ARCHITECTURE_PROPOSAL.md` are planning documents written in Turkish and Azerbaijani; everything else in the repository is English.

## Roadmap

1. **M1** — repository skeleton, security baseline, CI, Docker Compose (in progress).
2. **M2** — sign-in, organization model, onboarding.
3. **M3** — connectors, ingestion, de-duplication.
4. **M4** — LLM layer, personal feed, "Try this".
5. **M5** — hardening, load tests, pilot readiness.

The task list is in [TASKS.md](TASKS.md) and the milestone status in [PROGRESS.md](PROGRESS.md).

## License

Apache License 2.0. See [LICENSE](LICENSE). Contributions are accepted under the same license with a DCO sign-off; no CLA is required.
