# polymarket-bot — conservative automated trading for Polymarket "BTC Up or Down 5m"

> **Status: PAPER / RESEARCH.** Live trading is **locked by default** and has
> **never been exercised against the real exchange**. The live adapter is
> implemented against the official SDK source but is **NOT VERIFIED**
> end-to-end. Nothing in this repository funds a wallet or moves money.
>
> **Jurisdiction:** Polymarket blocks trading from many countries (France, the
> US, the UK, Germany, Italy, … — see [docs/research.md §6](docs/research.md)).
> The live lock refuses to start in a blocked jurisdiction and this project
> contains no way around geographic restrictions. Check your eligibility
> before doing anything beyond paper trading.

## What it does

A deterministic trading system for the 5-minute BTC Up/Down markets, with Claude
as an optional *reviewer*:

```
market data ─► validated markets ─► features ─► fair value ─► edge (after fees/slippage/exit)
                                                                   │
            Claude review (optional, can only veto/tighten) ◄──────┤
                                                                   ▼
             independent Risk Engine (40+ checks, hard caps) ─► Execution (FAK, idempotent)
                                                                   │
  Exit Engine (never waits for Claude) ◄─ portfolio ◄─ fills ◄─────┘
  Watchdog · Reconciliation · Kill switch · Audit log · Recorder · Replay
```

* **Fail closed everywhere**: stale/missing/contradictory data ⇒ no trade;
  unknown order state ⇒ HALTED; invariant breach or loss limit ⇒ KILL_SWITCH.
* **Claude ≠ risk ≠ execution ≠ wallet**: Claude can approve, reject or tighten
  a pre-validated candidate; it cannot size, change limits, execute or see
  secrets ([AGENTS.md](AGENTS.md), [docs/security.md](docs/security.md)).
* **Exact market rules**: a market is traded only if its verbatim resolution
  text matches a registered, enabled rule version (currently the Chainlink
  60-second TWAP rule effective 2026-08-14).
* **Same code path** for paper, replay and live; replay is lookahead-free and
  deterministic.

## Quick start

```bash
make install                 # uv sync --all-extras + pre-commit
make check                   # ruff, mypy --strict, bandit, secret scan, 450+ tests

# Offline pipeline on SYNTHETIC data (no network; results are not evidence)
make synth                   # 288 synthetic windows -> data/synthetic/session
make backtest                # replay + metrics + robustness -> reports/backtest
make walk-forward

# Paper trading on live public data (needs network access to Polymarket)
make paper                   # or: python -m polymarket_bot.app --mode paper
make status                  # read-only status from the local state DB
make diagnose                # read-only decision-pipeline report: why (no) trades (docs/diagnostics.md)
make kill-switch             # engage the kill switch immediately

# Restricted MCP server for Claude (stdio; read-only + proposals)
make mcp

# Live: prints the checklist, never enables anything
make live-readiness
```

## Modes

| Mode | Data | Execution | How |
|---|---|---|---|
| `disabled` (default) | — | — | default in `.env.example` |
| `replay` | recorded or SYNTHETIC session | paper exchange | `--mode replay --input <session>` |
| `paper` | live public data | paper exchange | `--mode paper` |
| `live` | live public data | Polymarket CLOB | `--mode live` — **locked**: env flags + policy-bound confirmation + promotion approval + compliance + reconciliation + healthy data + credentials all required |

## Repository layout

```
configs/            paper.yaml, live.example.yaml, risk_policy.example.yaml
docs/               architecture, security, trading, risk, deployment,
                    backtesting, incident-response, research, progress, validation
src/polymarket_bot/
  domain/ config/ ports.py         types, clock, config models, interfaces
  adapters/                        public REST/WS, paper exchange, live venue (SDK)
  market/ data/ features/          books, discovery, reference prices, recorder, replay
  strategies/btc_5m/ signals/      resolution rules, fair value, edge
  risk/ execution/ exits/ portfolio/
  llm/ mcp_server/                 Claude review, restricted MCP server
  lifecycle/ watchdog/ reconciliation/ promotion/ security/ audit/ storage/ monitoring/
  app/                             core step, assembly, replay engine, runners, CLI
  research/                        SYNTHETIC generator, backtest, walk-forward (never
                                   imported by production code)
tests/              unit, integration, replay, risk, execution, security
```

## Documentation

* [docs/progress.md](docs/progress.md) — what is IMPLEMENTED / TESTED / VERIFIED / NOT VERIFIED / TODO
* [docs/final-report.md](docs/final-report.md) — engineering report
* [docs/diagnostics.md](docs/diagnostics.md) — decision-pipeline counters, `make diagnose`, the 2026-09-25 zero-trade diagnosis
* [docs/research.md](docs/research.md) — verified external facts (APIs, fees, resolution, geo)
* [docs/architecture.md](docs/architecture.md) · [docs/trading.md](docs/trading.md) ·
  [docs/risk.md](docs/risk.md) · [docs/security.md](docs/security.md) ·
  [docs/deployment.md](docs/deployment.md) · [docs/backtesting.md](docs/backtesting.md) ·
  [docs/incident-response.md](docs/incident-response.md) · [docs/validation.md](docs/validation.md)
