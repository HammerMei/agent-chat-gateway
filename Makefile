.PHONY: install setup test lint start stop status clean help \
        e2e-up e2e-down e2e-test e2e-logs e2e-reset

RUNTIME_DIR := $(HOME)/.agent-chat-gateway
CONFIG      := $(RUNTIME_DIR)/config.yaml

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} \
	     /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install: ## Install dependencies (uv sync)
	uv sync

setup: ## Run the interactive setup wizard (idempotent — skips if config exists)
	uv run agent-chat-gateway onboard --repo-path "$(CURDIR)"

test: ## Run test suite
	uv run pytest tests/ -v --tb=short

lint: ## Run ruff check (if installed)
	@if command -v ruff >/dev/null 2>&1; then \
	    ruff check gateway/ tests/; \
	elif uv run ruff --version >/dev/null 2>&1; then \
	    uv run ruff check gateway/ tests/; \
	else \
	    echo "ruff not installed — skipping lint"; \
	fi

start: ## Start daemon
	uv run agent-chat-gateway start

stop: ## Stop daemon
	uv run agent-chat-gateway stop

status: ## Show daemon status
	uv run agent-chat-gateway status

clean: ## Remove __pycache__, .coverage, dist/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	rm -f .coverage coverage.json .coverage.*
	rm -rf dist/ build/ *.egg-info

# =============================================================================
# E2E Test Targets
# Requires: Docker, ANTHROPIC_API_KEY (for Claude Code)
# =============================================================================

E2E_COMPOSE := tests/e2e/docker-compose.yml
E2E_RC_URL  := http://localhost:3100

e2e-up: ## Start RC + ACG for E2E tests (idempotent)
	@echo "==> Starting MongoDB + Rocket.Chat ..."
	docker compose -f $(E2E_COMPOSE) up -d mongodb rocketchat
	@echo "==> Running E2E setup (creating RC accounts) ..."
	uv run python tests/e2e/setup.py --rc-url $(E2E_RC_URL)
	@echo "==> Starting ACG ..."
	docker compose -f $(E2E_COMPOSE) up -d acg
	@echo "==> Done. Run 'make e2e-test' to execute the test suite."

e2e-test: ## Run E2E tests (requires e2e-up first, needs CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY)
	uv run pytest tests/e2e/ -v -s --timeout=180 \
	    --ignore=tests/unit --ignore=tests/integration

e2e-logs: ## Tail logs for all E2E containers
	docker compose -f $(E2E_COMPOSE) logs -f

e2e-down: ## Stop and remove all E2E containers and volumes
	docker compose -f $(E2E_COMPOSE) down -v

e2e-reset: e2e-down e2e-up ## Full reset: tear down, recreate, re-setup
