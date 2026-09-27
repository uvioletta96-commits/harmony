# ---------------------------------------------------------------------------
# Makefile — the commands you actually type.
# ---------------------------------------------------------------------------
# `make help` lists everything. Targets are grouped by intent rather than by
# tool, because "how do I run this locally" is a different question from
# "how do I run the linter".

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON        ?= python3
VENV          ?= .venv
BIN           := $(VENV)/bin
BACKEND       := backend
COMPOSE       ?= docker compose
PROJECT       ?= harmony

export PYTHONPATH := $(BACKEND)

# Colours are disabled when stdout is not a terminal, so CI logs stay clean.
BOLD  := $(shell tput bold 2>/dev/null || true)
DIM   := $(shell tput dim 2>/dev/null || true)
RESET := $(shell tput sgr0 2>/dev/null || true)

.PHONY: help
help: ## Show this help
	@printf "$(BOLD)Гармония$(RESET) — available targets\n\n"
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@printf "\n$(DIM)Run 'make <target>' for details.$(RESET)\n"

# ---------------------------------------------------------------- Setup -----
.PHONY: install
install: ## Create the virtualenv and install all dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r $(BACKEND)/requirements-dev.txt
	@printf "\n$(BOLD)Done.$(RESET) Next: cp .env.example .env && make dev\n"

.PHONY: dev
dev: ## Run the API with the reloader against SQLite and no Redis
	cd $(BACKEND) && ../$(BIN)/flask --app wsgi:application run --debug --port 8000

.PHONY: dev-celery
dev-celery: ## Run a Celery worker in the foreground
	cd $(BACKEND) && CELERY_ALWAYS_EAGER=false ../$(BIN)/celery -A celery_worker.celery_app worker --loglevel=INFO

# ------------------------------------------------------------- Database -----
.PHONY: init-db
init-db: ## Create the schema directly (no migration history)
	cd $(BACKEND) && ../$(BIN)/flask --app wsgi:application init-db

.PHONY: migrate
migrate: ## Apply database migrations
	cd $(BACKEND) && ../$(BIN)/flask --app wsgi:application db upgrade

.PHONY: migrate-new
migrate-new: ## Autogenerate a migration: make migrate-new m="add reactions"
	@test -n "$(m)" || { echo "Usage: make migrate-new m=\"short description\""; exit 1; }
	cd $(BACKEND) && ../$(BIN)/flask --app wsgi:application db migrate -m "$(m)"

.PHONY: seed
seed: ## Populate a development database with sample content
	cd $(BACKEND) && ../$(BIN)/flask --app wsgi:application seed-demo

.PHONY: routes
routes: ## List every registered route
	cd $(BACKEND) && ../$(BIN)/flask --app wsgi:application routes

# --------------------------------------------------------------- Quality ----
.PHONY: test
test: ## Run the test suite
	cd $(BACKEND) && ../$(BIN)/pytest -q

.PHONY: test-fast
test-fast: ## Run the test suite, stopping at the first failure
	cd $(BACKEND) && ../$(BIN)/pytest -q -x

.PHONY: test-cov
test-cov: ## Run tests with a coverage report
	cd $(BACKEND) && ../$(BIN)/pytest --cov=app --cov-report=term-missing --cov-report=html

.PHONY: lint
lint: ## Check formatting and common errors
	$(BIN)/ruff check $(BACKEND)/app $(BACKEND)/tests tools
	$(BIN)/ruff format --check $(BACKEND)/app $(BACKEND)/tests

.PHONY: format
format: ## Apply formatting automatically
	$(BIN)/ruff format $(BACKEND)/app $(BACKEND)/tests tools
	$(BIN)/ruff check --fix $(BACKEND)/app $(BACKEND)/tests tools

.PHONY: typecheck
typecheck: ## Run static type checks (non-blocking by design)
	-$(BIN)/mypy $(BACKEND)/app --ignore-missing-imports --no-strict-optional

.PHONY: check
check: lint test check-routes check-frontend ## Everything that must pass before pushing

# ------------------------------------------------------------------ Docker --
.PHONY: up
up: ## Start the full stack
	$(COMPOSE) up -d --build db redis backend worker beat web
	@printf "\n$(BOLD)Starting.$(RESET) Apply migrations with: make migrate-docker\n"

.PHONY: down
down: ## Stop the stack (data volumes are preserved)
	$(COMPOSE) down

.PHONY: destroy
destroy: ## Stop the stack and DELETE ALL DATA
	@printf "\n$(BOLD)This deletes every database, every upload and all Redis keys.$(RESET)\n"
	@read -p "Type 'yes' to continue: " answer; [ "$$answer" = "yes" ] || { echo "Aborted."; exit 1; }
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Follow logs from every service
	$(COMPOSE) logs -f --tail=100

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: migrate-docker
migrate-docker: ## Apply migrations inside the container stack
	$(COMPOSE) run --rm migrate

.PHONY: seed-docker
seed-docker: ## Seed sample data inside the container stack
	$(COMPOSE) run --rm cli seed-demo

.PHONY: shell
shell: ## Open a Flask shell inside the container
	$(COMPOSE) run --rm cli shell

.PHONY: create-admin
create-admin: ## Create an administrator: make create-admin
	$(COMPOSE) run --rm cli create-admin

.PHONY: backup
backup: ## Take a logical PostgreSQL backup now
	@mkdir -p backups
	$(COMPOSE) exec -T db pg_dump -U harmony -d harmony -Fc > backups/harmony-$$(date -u +%Y%m%dT%H%M%SZ).dump
	@ls -lh backups/ | tail -5

.PHONY: restore
restore: ## Restore from a dump: make restore f=backups/harmony-....dump
	@test -n "$(f)" || { echo "Usage: make restore f=backups/harmony-20240101T000000Z.dump"; exit 1; }
	$(COMPOSE) exec -T db pg_restore -U harmony -d harmony --clean --if-exists < "$(f)"

.PHONY: observability
observability: ## Start Prometheus and Grafana
	$(COMPOSE) --profile observability up -d prometheus grafana
	@printf "Grafana: http://localhost:3000\nPrometheus: http://localhost:9090\n"

# ----------------------------------------------------------------- Config ---
.PHONY: secret
secret: ## Generate a strong random secret
	cd $(BACKEND) && ../$(BIN)/flask --app wsgi:application generate-secret

.PHONY: check-config
check-config: ## Print the effective configuration, secrets redacted
	cd $(BACKEND) && ../$(BIN)/flask --app wsgi:application show-config

.PHONY: openapi
openapi: ## Regenerate backend/app/static/openapi.json from spec/openapi.yaml
	$(PYTHON) tools/build_openapi.py

.PHONY: openapi-check
openapi-check: ## Fail if the generated OpenAPI document is stale
	$(PYTHON) tools/build_openapi.py --check

.PHONY: check-routes
check-routes: ## Fail if the spec and the route table have drifted apart
	$(PYTHON) tools/check_routes.py

.PHONY: check-frontend
check-frontend: ## Fail on a broken import or an unreviewed innerHTML sink
	$(PYTHON) tools/check_frontend.py

.PHONY: icons
icons: ## Regenerate the PNG brand assets from logo.svg
	$(PYTHON) tools/generate_icons.py

.PHONY: clean
clean: ## Remove caches and build artefacts
	find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '.pytest_cache' -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BACKEND)/.coverage $(BACKEND)/htmlcov .ruff_cache $(BACKEND)/.ruff_cache
	@echo "Cleaned."

.PHONY: flush-cache
flush-cache: ## Drop every cached key
	cd $(BACKEND) && ../$(BIN)/flask --app wsgi:application flush-cache
