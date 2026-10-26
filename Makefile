.DEFAULT_GOAL := help
COMPOSE := docker compose -f deploy/docker-compose.yml

.PHONY: help dev test lint fmt migrate hooks secrets-scan

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

dev: ## Start the local stack (needs deploy/ from M1-08)
	$(COMPOSE) --profile dev up

test: ## Run backend and frontend tests
	uv run pytest
	pnpm --dir frontend test

lint: ## Run linters and type checks
	uv run ruff check .
	uv run mypy backend/src
	uv run lint-imports
	pnpm --dir frontend lint
	pnpm --dir frontend typecheck

fmt: ## Format code
	uv run ruff format .
	pnpm --dir frontend exec prettier --write .

hooks: ## Enable the git pre-commit secret scan
	git config core.hooksPath scripts/git-hooks

secrets-scan: ## Scan the full git history for secrets (gitleaks in Docker)
	docker run --rm -v "$(CURDIR):/repo:ro" zricethezav/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f detect --source /repo --redact --verbose

migrate: ## Apply database migrations (ask first)
	uv run alembic -c backend/alembic.ini upgrade head
