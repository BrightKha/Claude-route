# Deployment

## Requirements

* Python 3.12 (3.13 supported by the dependency set), `uv` 0.8.x.
* Network access to `gamma-api.polymarket.com`, `clob.polymarket.com`,
  `ws-subscriptions-clob.polymarket.com`, `ws-live-data.polymarket.com`
  (paper/record/live) and `api.anthropic.com` (only if `llm.mode != off`).
* A jurisdiction where Polymarket permits trading, for anything beyond
  paper trading (docs/research.md §6). Paper trading uses public data only.

## Install and verify

```bash
make install        # uv sync --all-extras, pre-commit hooks
make check          # must be green before any deployment
make audit          # pip-audit (needs network to PyPI/OSV)
```

## Configuration

* YAML in `configs/` (strict schema, unknown keys rejected, secret-like keys
  rejected). `configs/paper.yaml` references `risk_policy.example.yaml`.
* Environment (see `.env.example`; never commit a real `.env`):
  `TRADING_MODE`, `LIVE_TRADING_ENABLED`, `LIVE_CONFIRMATION`,
  `OPERATOR_JURISDICTION`, `BOT_DATA_DIR`, `LOG_LEVEL`, and secrets
  `ANTHROPIC_API_KEY` (optional), `POLYMARKET_PRIVATE_KEY`,
  `POLYMARKET_WALLET_ADDRESS`, `POLYMARKET_API_KEY/SECRET/PASSPHRASE` (live only).
  The process never reads `.env` files itself; inject variables with your
  secret manager / service manager.

## Paper trading

```bash
python -m polymarket_bot.app --config configs/paper.yaml paper     # or --mode paper
python -m polymarket_bot.app --config configs/paper.yaml status
```

Data (state DB, audit log, recordings, kill-switch sentinel) goes to
`data_dir` (`./data` by default, `/data` in Docker). Recordings are replayable
with `replay`/`backtest`. Stop with SIGINT/SIGTERM: the runner cancels open
orders (none rest under the FAK policy), publishes final status and closes the
recorder.

## Docker

`Dockerfile` (python:3.12-slim, non-root uid 10001, uv-pinned, frozen lock,
live extra **not** installed by default) and `docker-compose.yml` (bot + MCP
services, read-only root FS, dropped capabilities, metrics bound to
127.0.0.1). **Status: written, NOT built or run in the development
environment (no Docker daemon available there).**

```bash
docker compose up -d bot
docker compose run --rm mcp          # stdio MCP server for a local client
```

## MCP server for Claude

Run it as a **separate process without any secret in its environment** (it
refuses to start otherwise). Example client entry (stdio):

```json
{"command": "python", "args": ["-m", "polymarket_bot.app", "--config",
 "configs/paper.yaml", "mcp-server"]}
```

## Monitoring

Set `monitoring.metrics_enabled: true` to expose Prometheus metrics on
`127.0.0.1:9108` (`bot_state`, `bot_kill_switch_engaged`, equity, risk equity,
exposure, positions, open/unknown orders, watchdog anomalies, counters). Alert
on: kill switch engaged, state HALTED for > 5 min, unknown orders > 0,
watchdog anomalies > 0, no decision step for > 10 s. JSON logs can be enabled
through `setup_logging(json_path=...)`.

## Live procedure (do NOT skip steps)

Live has never been exercised by the developers of this repository. Treat the
first live session as a test of the software, with money you can lose.

1. Confirm eligibility (jurisdiction) — the bot refuses blocked countries.
2. Create a **dedicated** wallet that holds only the working capital; start
   flat (no positions, no open orders). Never reuse a personal wallet.
3. Collect evidence: non-synthetic backtest, out-of-sample report, ≥ 14 days
   of paper trading, kill-switch and reconciliation drills, test and security
   runs — record them with the promotion tooling (docs/risk.md).
4. Write a SMALL_LIVE-compliant risk policy (within SMALL_LIVE caps),
   `configs/live.yaml` from `live.example.yaml`, `strategy.enabled: true`.
5. `promotion approve --stage SMALL_LIVE --phrase <exact phrase> --operator <you>`.
6. Install the live extra, set `TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true`,
   `LIVE_CONFIRMATION=I-ACCEPT-LIVE-RISK-<policy hash[:16]>`,
   `OPERATOR_JURISDICTION=<ISO code>`, and the Polymarket credentials.
7. `make live-readiness` — every static check must PASS (reconciliation and
   market-data checks are proven by the live runner at startup).
8. `python -m polymarket_bot.app --config configs/live.yaml --mode live`, watch
   the first orders manually, keep `make kill-switch` at hand.
9. Redeem resolved winnings on-chain yourself (automatic redemption is not
   implemented); the bot excludes pending redemptions from reconciliation.
