.PHONY: install setup test coverage lint start stop status clean help \
        e2e-up e2e-down e2e-test e2e-logs e2e-reset

RUNTIME_DIR := $(HOME)/.agent-chat-gateway
CONFIG      := $(RUNTIME_DIR)/config.yaml

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} \
	     /^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install: ## Install dependencies (uv sync)
	uv sync

setup: ## Run the interactive setup wizard (idempotent — skips if config exists)
	uv run agent-chat-gateway onboard --repo-path "$(CURDIR)"

test: ## Run test suite
	uv run pytest tests/ -v --tb=short

coverage: ## Run the test suite with branch coverage and print the gaps
	uv run pytest tests/unit tests/integration -q --timeout=120 \
		--cov=gateway --cov-branch --cov-report=term-missing:skip-covered

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
E2E_MM_URL  := http://localhost:8065

# Both platforms must be bootstrapped BEFORE ACG starts, and the ordering is
# not something compose can express. `depends_on: service_healthy` orders
# CONTAINERS; it says nothing about whether the `acg_bot` ACCOUNT exists yet.
# Each connector logs in as that account at startup, so ACG started against a
# fresh, unseeded platform fails to authenticate on the connector that lost
# the race — which surfaces as every test on that platform timing out, not as
# a login error.
e2e-up: ## Start RC + Mattermost + ACG for E2E tests (idempotent)
	@echo "==> Starting MongoDB + Rocket.Chat ..."
	docker compose -f $(E2E_COMPOSE) up -d mongodb rocketchat
	@echo "==> Starting Postgres + Mattermost ..."
	docker compose -f $(E2E_COMPOSE) up -d postgres mattermost
	@echo "==> Running RC setup (creating RC accounts) ..."
	uv run python tests/e2e/setup.py --rc-url $(E2E_RC_URL)
	@echo "==> Running MM setup (creating MM team + accounts) ..."
	uv run python tests/e2e/mm_setup.py --mm-url $(E2E_MM_URL)
	@echo "==> Starting ACG ..."
	docker compose -f $(E2E_COMPOSE) up -d acg
	@echo "==> Done. Run 'make e2e-test' to execute the test suite."

e2e-test: ## Run E2E tests (requires e2e-up first, needs CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY)
	@docker container inspect acg-e2e >/dev/null 2>&1 || { \
	    echo "ERROR: container 'acg-e2e' is not up — run 'make e2e-up' first."; \
	    echo "       'make e2e-test' only runs the suite; it starts nothing."; \
	    exit 1; }
	@# The token that matters is the one INSIDE the container, not the one in
	@# this shell. Compose bakes environment in at container CREATION, so a
	@# stack brought up without a token stays without one however the test run
	@# is invoked — and the old check, which read this shell, said nothing
	@# while every Claude test timed out for eight minutes and reported
	@# "no matching post", as if delivery were broken.
	@docker exec acg-e2e sh -c 'test -n "$$CLAUDE_CODE_OAUTH_TOKEN$$ANTHROPIC_API_KEY"' 2>/dev/null || { \
	    echo "ERROR: the running acg-e2e container has NO Claude credentials."; \
	    echo "       Exporting them now does not help: the container was created"; \
	    echo "       without them, and compose fixes environment at creation."; \
	    echo "       Recreate just this container — the platforms and their"; \
	    echo "       seeded accounts survive, so this costs seconds, not a"; \
	    echo "       Rocket.Chat boot:"; \
	    echo "         CLAUDE_CODE_OAUTH_TOKEN=... docker compose -f $(E2E_COMPOSE) \\"; \
	    echo "             up -d --force-recreate acg"; \
	    echo "       (or ANTHROPIC_API_KEY=...). Reach for 'make e2e-down' only"; \
	    echo "       if you also want the platform data gone — it takes -v."; \
	    echo "       Every Claude test fails as a 120s timeout otherwise, which"; \
	    echo "       reads like a delivery bug."; \
	    exit 1; }
	uv run pytest tests/e2e/ -v -s --timeout=180 \
	    --ignore=tests/unit --ignore=tests/integration

e2e-logs: ## Tail logs for all E2E containers
	docker compose -f $(E2E_COMPOSE) logs -f

e2e-shell: ## Shell into a running E2E container (S=acg|rocketchat|mongodb|mattermost|postgres, default acg)
	docker compose -f $(E2E_COMPOSE) exec $(or $(S),acg) bash

e2e-acg: ## Run an ACG command inside the container (e.g. make e2e-acg C="list")
	@test -n "$(C)" || (echo "usage: make e2e-acg C=\"list\"" && exit 1)
	docker compose -f $(E2E_COMPOSE) exec acg agent-chat-gateway $(C)

e2e-dump: ## Write full container logs + state to ./e2e-logs (same set CI uploads)
	@mkdir -p e2e-logs
	@for c in acg-e2e acg-e2e-rocketchat acg-e2e-mongodb \
	          acg-e2e-mattermost acg-e2e-postgres; do \
	    docker logs $$c > e2e-logs/$$c.log 2>&1 || true; \
	done
	@docker compose -f $(E2E_COMPOSE) ps > e2e-logs/ps.txt 2>&1 || true
	@docker compose -f $(E2E_COMPOSE) config > e2e-logs/resolved-compose.yml 2>&1 || true
	@docker exec acg-e2e sh -c 'cat /root/.agent-chat-gateway/gateway.log' \
	    > e2e-logs/acg-gateway.log 2>&1 || true
	@docker exec acg-e2e agent-chat-gateway list --all > e2e-logs/acg-list.txt 2>&1 || true
	@echo "==> Wrote e2e-logs/ ($$(ls e2e-logs | wc -l | tr -d ' ') files)"

e2e-probe: ## Re-verify the RC platform behaviour design §6 depends on, against the running stack
	uv run python scripts/probe_a1_rc.py \
	    --url $(E2E_RC_URL) \
	    --probe-user test_user --probe-password test_user_e2e_2024 \
	    --admin-user admin --admin-password admin_e2e_2024 \
	    --member-room acg-e2e-claude --outside-room acg-e2e-outside

# The probe answers a different question from the E2E test, which is why both
# exist: this one asks what MATTERMOST does (no ACG involved), and is what to
# reach for when a version bump makes the pin guard fail. The E2E test asks
# whether ACG honours it. A probe pass with an E2E failure means the runtime
# regressed; both failing means the platform changed under us.
#
# NOTE: this drives the acg_bot ACCOUNT — the same one the connector uses —
# and its membership isolation case JOINS the bot to the outside channel and
# removes it again. A clean run leaves membership as it found it; a run killed
# partway can leave the bot inside, which is precisely the state
# test_mm_membership_delivery.py refuses to run in. `mm_setup.py` puts it
# back, and the test says so when it trips.
e2e-probe-mm: ## Re-verify the Mattermost platform behaviour design §6.2/§6.3 depends on
	uv run python scripts/probe_a2_mm.py \
	    --url $(E2E_MM_URL) --team acg-e2e \
	    --probe-user acg_bot --probe-password acg_bot_e2e_2024 \
	    --admin-user mmadmin --admin-password mmadmin_e2e_2024 \
	    --member-channel acg-e2e-mm-claude --outside-channel acg-e2e-mm-outside

e2e-down: ## Stop and remove all E2E containers and volumes
	docker compose -f $(E2E_COMPOSE) down -v

e2e-reset: e2e-down e2e-up ## Full reset: tear down, recreate, re-setup
