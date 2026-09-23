# CLAUDE.md — rules for Claude (and any AI agent) working in this repository

This repository is a **real-money trading system**. It is DISABLED/PAPER by
default. Treat every change as safety-critical.

## Non-negotiable invariants

1. **CLAUDE ≠ RISK ENGINE ≠ EXECUTION ENGINE ≠ WALLET.** Claude may analyse,
   review, approve, reject or propose. Deterministic code decides risk, size,
   execution and exits.
2. Never add code that lets an LLM (or the MCP server) change risk limits,
   hard caps, the trading mode, live flags, the kill switch, promotion stages,
   wallet/credentials, or submit raw orders. `tests/security/` enforces this —
   never weaken those tests.
3. **Fail closed.** Missing, stale, ambiguous or contradictory data ⇒
   `NO_TRADE`. Unknown order state ⇒ `HALTED`. Prefer HALT to uncertain
   execution.
4. **No secrets anywhere** except the process environment of the two adapters
   that need them (`adapters/polymarket_live.py`, `llm/claude_client.py`).
   Never log, print, prompt, or persist a secret. Never read `.env` yourself.
5. **No lookahead.** Features, fair value, execution and exits may only use
   state built from events with receive time ≤ decision time.
6. **No magic numbers.** Limits live in `config/risk_policy.py` (with hard caps
   in `risk/hard_caps.py`) or YAML under `configs/`, documented in
   `docs/risk.md`.
7. **LIVE stays locked** unless every live-lock precondition passes
   (`promotion/live_lock.py`). Never mark anything `LIVE_READY` without evidence.

## How to work here

- Inspect before modifying; run `make check` (lint, mypy --strict, bandit,
  secret scan, tests) before claiming anything works.
- Never disable, skip or loosen a test to make it pass. Never simulate an API
  success; fakes must be named `Fake*`/`Paper*`/`Replay*` and flagged.
- External facts (API shapes, fees, resolution rules) must be traceable to
  `docs/research.md`. If the official source changed, update research first.
- Resolution rules are versioned (`strategies/btc_5m/resolution.py`). A market
  whose text/config does not match an enabled rule version is not tradable.
- Research code (`src/polymarket_bot/research/`, `research/`) must never be
  imported by production modules; promotion to production is manual.
- Keep `docs/progress.md` current, distinguishing IMPLEMENTED / TESTED /
  VERIFIED / NOT VERIFIED / TODO.

## Commands

```
make install      # uv sync --all-extras + pre-commit
make check        # lint + typecheck + security + tests
make synth replay backtest   # offline pipeline on SYNTHETIC data
make status       # read-only status
```
