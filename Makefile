.DEFAULT_GOAL := help
COMPOSE := docker compose -f deploy/docker-compose.yml --env-file deploy/.env

.PHONY: help env dev down worker beat test test-e2e test-integration test-compose test-sso-dev lint fmt migrate hooks secrets-scan

help: ## Show available targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-17s %s\n", $$1, $$2}'

env: ## Create deploy/.env with generated passwords (refuses to overwrite)
	bash scripts/init-env.sh

dev: ## Build and start the whole stack with Docker Compose (run `make env` first)
	$(COMPOSE) --profile dev up --build

down: ## Stop the stack (keeps the data volumes)
	$(COMPOSE) down

worker: ## Run a Celery worker (needs Redis, configured through PHAROS_* variables)
	uv run celery --app pharos.workers.entrypoint worker --loglevel INFO

beat: ## Run the Celery scheduler (needs Redis)
	uv run celery --app pharos.workers.entrypoint beat --loglevel INFO

test: ## Run hook, backend and frontend unit tests
	bash tests/hooks/test_guard.sh
	bash tests/scripts/test_init_env.sh
	bash tests/scripts/test_check_dco.sh
	uv run pytest backend/tests
	pnpm --dir frontend test

test-e2e: ## Run Playwright against the production build (needs: pnpm --dir frontend exec playwright install chromium)
	pnpm --dir frontend test:e2e

test-integration: ## Run integration tests against throwaway PostgreSQL and Redis (needs Docker)
	bash scripts/test-infra.sh run

test-compose: ## Build and check the Compose stack in a throwaway project (needs Docker, takes minutes)
	bash scripts/compose-smoke.sh

test-sso-dev: ## Check the development identity provider setup (Keycloak behind Caddy; needs Docker)
	bash scripts/sso-dev-check.sh

lint: ## Run linters and type checks
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy --config-file backend/pyproject.toml backend/src
	uv run lint-imports --config backend/.importlinter
	pnpm --dir frontend lint
	pnpm --dir frontend typecheck

fmt: ## Format code
	uv run ruff format .
	@if [ -f frontend/package.json ]; then pnpm --dir frontend exec prettier --write .; fi

hooks: ## Enable the git pre-commit secret scan
	git config core.hooksPath scripts/git-hooks

secrets-scan: ## Scan the full git history for secrets (gitleaks in Docker)
	docker run --rm -v "$(CURDIR):/repo:ro" zricethezav/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f git --redact --verbose /repo

migrate: ## Apply database migrations (ask first)
	uv run alembic -c backend/alembic.ini upgrade head
