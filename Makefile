.PHONY: install setup test lint start stop status clean help

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
