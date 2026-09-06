# Root task runner. `make help` lists every target.
# Backend tasks run on the host via uv (fast feedback); the stack itself runs
# in Docker Compose. Host tasks that touch the database expect `make up-db`.

SHELL := /bin/bash
BACKEND := backend
FRONTEND := frontend
COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help dev rebuild down logs up-db check-db install migrate migration downgrade \
        test test-backend test-frontend e2e lint lint-backend lint-frontend \
        format format-check typecheck typecheck-backend typecheck-frontend \
        openapi generate-client clean

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Stack -------------------------------------------------------------------

dev: ## Build and start the full stack (api, web, postgres, mailpit)
	$(COMPOSE) up --build

rebuild: ## Rebuild and restart after adding a dependency
	$(COMPOSE) up -d --build --force-recreate --renew-anon-volumes
	@echo "note: --renew-anon-volumes discards the container's cached .venv/node_modules"

down: ## Stop the stack and remove containers
	$(COMPOSE) down

logs: ## Follow logs from all services
	$(COMPOSE) logs -f

up-db: ## Start only PostgreSQL (for host-side migrations and tests)
	$(COMPOSE) up -d postgres
	@until $(COMPOSE) exec -T postgres pg_isready -q; do sleep 1; done
	@$(MAKE) --no-print-directory check-db

check-db: ## Check that the database in DATABASE_URL is reachable
	@cd $(BACKEND) && uv run python -m scripts.check_db

install: ## Install backend and frontend dependencies from lockfiles
	cd $(BACKEND) && uv sync --frozen
	cd $(FRONTEND) && npm ci

# --- Database ----------------------------------------------------------------

migrate: check-db ## Apply all migrations to the database in DATABASE_URL
	cd $(BACKEND) && uv run alembic upgrade head

migration: check-db ## Autogenerate a migration: make migration name="add users"
	@test -n "$(name)" || { echo 'usage: make migration name="add users"'; exit 1; }
	cd $(BACKEND) && uv run alembic revision --autogenerate -m "$(name)"

downgrade: check-db ## Roll back one migration
	cd $(BACKEND) && uv run alembic downgrade -1

# --- Quality gates -----------------------------------------------------------

test: test-backend test-frontend ## Run backend and frontend test suites

test-backend: check-db ## Run pytest
	cd $(BACKEND) && uv run pytest

test-frontend: ## Run Vitest
	cd $(FRONTEND) && npm test

e2e: ## Run Playwright end-to-end tests (wired up in Phase 9)
	@echo "Playwright is set up in Phase 9 -- see PLAN.md."

lint: lint-backend lint-frontend ## Lint everything

lint-backend:
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .

lint-frontend:
	cd $(FRONTEND) && npm run lint && npm run format:check

format: ## Auto-format everything
	cd $(BACKEND) && uv run ruff format . && uv run ruff check --fix .
	cd $(FRONTEND) && npm run format

format-check: lint ## Alias for lint

typecheck: typecheck-backend typecheck-frontend ## Type-check everything

typecheck-backend:
	cd $(BACKEND) && uv run mypy app

typecheck-frontend:
	cd $(FRONTEND) && npm run typecheck

# --- API contract ------------------------------------------------------------

openapi: ## Export the OpenAPI schema to ./openapi.json
	cd $(BACKEND) && uv run python -m scripts.export_openapi ../openapi.json

generate-client: openapi ## Regenerate the typed frontend API client (wired up in Phase 3)
	@echo "Client generation is set up in Phase 3 -- see PLAN.md."

clean: ## Remove build artefacts and caches
	rm -rf $(FRONTEND)/dist $(FRONTEND)/coverage
	find $(BACKEND) -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache
