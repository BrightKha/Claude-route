.DEFAULT_GOAL := help
UV ?= uv
PY := $(UV) run python
CONFIG ?= configs/paper.yaml
INPUT ?= data/synthetic/session
REPORT ?= reports/backtest

.PHONY: help install lint format typecheck test test-fast security secrets-scan audit check \
        synth replay backtest walk-forward paper record status diagnose ptb-validate \
        live-readiness kill-switch mcp clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

install: ## Create the venv and install all extras + dev tools
	$(UV) sync --all-extras
	$(UV) run pre-commit install || true

lint: ## Ruff lint + format check
	$(UV) run ruff check src tests scripts
	$(UV) run ruff format --check src tests scripts

format: ## Auto-format
	$(UV) run ruff format src tests scripts
	$(UV) run ruff check --fix src tests scripts

typecheck: ## mypy --strict
	$(UV) run mypy

test: ## Full test suite
	$(UV) run pytest

test-fast: ## Tests without slow simulations
	$(UV) run pytest -m "not slow"

secrets-scan: ## Scan tracked files for secrets
	$(PY) scripts/check_secrets.py

audit: ## Dependency vulnerability audit (needs network to PyPI/OSV)
	$(UV) run pip-audit --skip-editable

security: secrets-scan ## Static security analysis + secret scan
	$(UV) run bandit -q -r src -c pyproject.toml

check: lint typecheck security test ## Everything a contributor must run before pushing

synth: ## Generate a SYNTHETIC dataset (for pipeline validation only)
	$(PY) -m polymarket_bot.app synth --out $(INPUT) --windows 288 --seed 7

replay: ## Replay a recorded/synthetic session through the paper exchange
	$(PY) -m polymarket_bot.app --config $(CONFIG) replay --input $(INPUT)

backtest: ## Backtest + robustness report
	$(PY) -m polymarket_bot.app --config $(CONFIG) backtest --input $(INPUT) --report $(REPORT) --robustness

walk-forward: ## Walk-forward calibration study
	$(PY) -m polymarket_bot.app --config $(CONFIG) walk-forward --input $(INPUT) --report $(REPORT)

paper: ## Paper trading on live public data (needs network access to Polymarket)
	$(PY) -m polymarket_bot.app --config $(CONFIG) paper

record: ## Record live public data only (no trading)
	$(PY) -m polymarket_bot.app --config $(CONFIG) record

status: ## Bot status from the local state DB
	$(PY) -m polymarket_bot.app --config $(CONFIG) status

diagnose: ## Read-only decision-pipeline diagnostic: why (no) trades
	$(PY) -m polymarket_bot.app --config $(CONFIG) diagnose

ptb-validate: ## Accumulate price-to-beat evidence from data/recordings (policy stays OFF)
	$(PY) -m polymarket_bot.app --config $(CONFIG) ptb-validate

live-readiness: ## Print the live-lock checklist (never enables live)
	$(PY) -m polymarket_bot.app --config configs/live.example.yaml live-readiness

kill-switch: ## Engage the kill switch immediately
	$(PY) -m polymarket_bot.app --config $(CONFIG) kill-switch engage --reason "manual via make"

mcp: ## Run the restricted MCP server over stdio
	$(PY) -m polymarket_bot.app --config $(CONFIG) mcp-server

clean: ## Remove caches
	rm -rf .mypy_cache .ruff_cache .pytest_cache .hypothesis
