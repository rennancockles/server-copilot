.PHONY: help \
        install pre-commit start dev \
        lint lint-fix ruff ruff-check ruff-fix format-check fmt \
        pre-commit-run pre-commit-sim \
        type-check mypy ty \
        test test_cov test-unit test-ci \
        security-scan \
        release-dry release \
        publish-docs docs \
        env-check clean

.DEFAULT_GOAL := help

# Load .env (if present) and export vars to all recipe subprocesses.
-include .env
export

# ── Variables ──────────────────────────────────────────────────────────────────
PROJECT_NAME    := Server Copilot
PKG_MANAGER     := uv
PYTEST_PARALLEL ?= -n auto

ifeq ($(PKG_MANAGER),poetry)
INSTALL  := poetry install --with dev,docs --all-extras
RUN      := poetry run
DOCS_RUN := poetry run
else
INSTALL  := uv sync --all-extras
RUN      := uv run
DOCS_RUN := uv run --group docs
endif

# ── Colors ─────────────────────────────────────────────────────────────────────
BOLD   := \033[1m
RED    := \033[31m
GREEN  := \033[32m
YELLOW := \033[33m
BLUE   := \033[34m
CYAN   := \033[36m
RESET  := \033[0m

# ── Help ───────────────────────────────────────────────────────────────────────
help: ## Show available commands
	@printf "$(BOLD)$(PROJECT_NAME) — Development Commands$(RESET)\n\n"
	@awk 'BEGIN {FS = ":.*##"} \
		/^##@/ { printf "\n\033[1;36m%s$(RESET)\n", substr($$0,5) } \
		/^[a-zA-Z_-]+:.*?## / { printf "  \033[34m%-20s\033[0m %s\n", $$1, $$2 }' \
		$(MAKEFILE_LIST)
	@echo ""

##@ Setup

install: ## Install dev + docs dependencies
	@printf "$(YELLOW)Installing dependencies…$(RESET)\n"
	@$(INSTALL)
	@printf "$(GREEN)✓ Done$(RESET)\n"

pre-commit: ## Install pre-commit hooks
	@printf "$(YELLOW)Installing pre-commit hooks…$(RESET)\n"
	@$(RUN) pre-commit install
	@printf "$(GREEN)✓ Hooks installed$(RESET)\n"

start: install pre-commit ## Bootstrap dev environment (install + hooks)

dev: start ## Alias for start

##@ Quality

lint: ruff-check format-check type-check ## Check linting, formatting, and types (read-only)

lint-fix: fmt ruff-fix ruff-check format-check type-check ## Auto-fix then verify all quality checks

ruff: ruff-check format-check ## Ruff lint + format check

ruff-check: ## Run Ruff lint check
	@printf "$(YELLOW)Ruff lint…$(RESET)\n"
	@$(RUN) ruff check .

ruff-fix: ## Run Ruff lint with auto-fix
	@printf "$(YELLOW)Ruff fix…$(RESET)\n"
	@$(RUN) ruff check . --fix

format-check: ## Check formatting (read-only)
	@printf "$(YELLOW)Format check…$(RESET)\n"
	@$(RUN) ruff format . --check

fmt: ## Auto-format code
	@printf "$(YELLOW)Formatting…$(RESET)\n"
	@$(RUN) ruff format .

type-check: mypy ## Run type checker (alias)

mypy: ## MyPy type check
	@printf "$(YELLOW)MyPy…$(RESET)\n"
	@$(RUN) mypy

ty: ## Ty type check (faster alternative to mypy)
	@printf "$(YELLOW)Ty check…$(RESET)\n"
	@$(RUN) ty check

pre-commit-run: ## Run pre-commit on all files
	@printf "$(YELLOW)Pre-commit (all files)…$(RESET)\n"
	@$(RUN) pre-commit run --all-files

pre-commit-sim: ## Simulate pre-commit locally (for debugging hooks)
	@printf "$(YELLOW)Simulating pre-commit…$(RESET)\n"
	@$(RUN) ruff check . --fix --exit-non-zero-on-fix \
		|| printf "$(YELLOW)⚠ Ruff applied fixes — re-run to verify$(RESET)\n"
	@$(RUN) ruff format .

##@ Testing

test: ## Run tests with coverage (parallel)
	@printf "$(YELLOW)Running tests…$(RESET)\n"
	@$(RUN) pytest $(PYTEST_PARALLEL) --cov --cov-report=term-missing:skip-covered --cov-report=html

test_cov: test ## Alias for test (with HTML coverage report)

test-unit: ## Run unit tests only
	@printf "$(YELLOW)Running unit tests…$(RESET)\n"
	@$(RUN) pytest $(PYTEST_PARALLEL) --cov --cov-report=term-missing:skip-covered --cov-report=html -m "unit"

test-ci: ## CI run: JUnit XML + coverage XML, excludes integration tests
	@printf "$(YELLOW)CI tests…$(RESET)\n"
	@$(RUN) pytest $(PYTEST_PARALLEL) \
		--cov --cov-report=term-missing --cov-report=html \
		--cov-report=xml:coverage.xml --cov-fail-under=75 \
		--junitxml=test-results.xml -m "not integration"

##@ Security

security-scan: ## Run gitleaks secret scan
	@printf "$(YELLOW)Security scan…$(RESET)\n"
	@$(RUN) gitleaks dir -v .

##@ Release

release-dry: ## Dry-run semantic release (prints next version)
	@printf "$(YELLOW)Semantic release dry run…$(RESET)\n"
	@$(RUN) semantic-release version --print

release: ## Cut a new release via semantic-release
	@printf "$(YELLOW)Releasing…$(RESET)\n"
	@$(RUN) semantic-release version

##@ Utilities

env-check: ## Print environment diagnostics
	@printf "$(BOLD)Environment$(RESET)\n"
	@printf "  $(BLUE)Python:$(RESET)      %s\n" "$$(python --version 2>/dev/null || echo 'not found')"
	@printf "  $(BLUE)Manager:$(RESET)     %s\n" "$(PKG_MANAGER)"
	@printf "  $(BLUE)Version:$(RESET)     %s\n" "$$( $(PKG_MANAGER) --version 2>/dev/null || echo 'not found')"
	@printf "  $(BLUE)pre-commit:$(RESET)  %s\n" "$$($(RUN) pre-commit --version 2>/dev/null || echo 'not found')"
	@if [ -f .env ]; then \
		printf "  $(GREEN)✓ .env present$(RESET)\n"; \
	else \
		printf "  $(YELLOW)⚠ .env missing — copy from .env.example$(RESET)\n"; \
	fi

clean: ## Remove build artifacts and caches
	@printf "$(YELLOW)Cleaning…$(RESET)\n"
	@find . -type d \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.mypy_cache' -o -name '.ruff_cache' \) \
		-not -path './.loom/*' -prune -exec rm -rf {} +
	@find . -type f -name '*.pyc' -not -path './.loom/*' -delete
	@rm -rf htmlcov coverage.xml test-results.xml .coverage
	@printf "$(GREEN)✓ Clean$(RESET)\n"
