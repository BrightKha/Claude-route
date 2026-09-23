# Final engineering report (Phase 9) — 2026-09-23

## 1. Verdict

The system is **ready for extended PAPER trading on live public data** and for
research on recorded sessions. It is **not LIVE_READY**: the live adapter and
the live bootstrap are implemented and unit-tested against the SDK's own
models, but have **never been verified against the real exchange**, no real
market session has been recorded or backtested, and the promotion pipeline
(≥ 200 non-synthetic backtest trades, out-of-sample evidence, ≥ 14 days of
paper trading, drills, operator approval) has not started. Live stays locked.

**Jurisdiction warning.** Polymarket blocks trading from France and ~39 other
countries (research §6). The live lock refuses a blocked jurisdiction; this
project contains no way around it. Operators must establish their own
eligibility before anything beyond paper trading.

## 2. Architecture (summary)

Deterministic pipeline with Claude as an optional reviewer — see
[architecture.md](architecture.md):
market data (resilient WS + Gamma + REST resync, recorded) → validated markets
(rule text hash) → features → TWAP-aware fair value with an uncertainty band →
executable, cost-aware edge → optional Claude review (veto/tighten only) →
independent Risk Engine (40+ checks, hard caps) → idempotent FAK execution →
portfolio → exits (never wait for Claude). Around it: watchdog + loop-stall
thread, reconciliation, kill switch, hash-chained audit log, SQLite state,
recorder/replay, Prometheus metrics, a read-only MCP server (separate process,
proposals only), promotion gates and a multi-layer live lock.

## 3. What was built (by area)

| Area | Main modules |
|---|---|
| Domain & config | `domain/*`, `config/*` (strict YAML, hashed risk policy, no secrets) |
| Market data | `adapters/ws_client.py`, `adapters/polymarket_public.py`, `market/*`, `data/*` |
| Strategy | `strategies/btc_5m/resolution.py`, `features/btc5m.py`, `strategies/btc_5m/fair_value.py`, `signals/edge.py`, `exits/engine.py` |
| Safety | `risk/*`, `lifecycle/*`, `watchdog/*`, `reconciliation/*`, `promotion/*`, `security/*`, `audit/*` |
| Execution | `execution/engine.py`, `adapters/paper_exchange.py`, `adapters/polymarket_live.py`, `portfolio/portfolio.py` |
| Claude / MCP | `llm/*`, `mcp_server/*` |
| Runtime | `app/core.py`, `app/build.py`, `app/replay_engine.py`, `app/runner.py`, `app/live.py`, `app/cli.py`, `monitoring/*` |
| Research | `research/synthetic.py`, `research/backtest.py`, `research/walk_forward.py`, `research/metrics.py` |

92 source modules (~12k lines), 453 tests (~5.5k lines): unit 246, risk 83,
execution 52, security 43, integration 23, replay 6.

## 4. Key decisions

D1–D10 in [research.md](research.md) plus: FAK-only execution (no verified
order heartbeat in the Python SDK); live venue built without the SDK's
wallet-readiness step (it can deploy a wallet); flat dedicated wallet required
at live start; conservative risk equity for loss limits; Claude off the
decision path with verdict cache; MCP as a separate secret-free process with a
read-only DB; synthetic data flagged and excluded from evidence.

## 5. Hypotheses (explicit, to be tested on real data)

* The Chainlink 60 s TWAP can be approximated from the RTDS spot feed within
  `model_error_bps` (3 bps), and the RTDS TWAP topic mirrors the settlement
  TWAP — **NOT VERIFIED on live data**.
* Short-horizon BTC log-price behaves locally like driftless Brownian motion
  with EWMA-estimated volatility; jumps are covered by the uncertainty band.
* Paper latency (150 ms ± 100 ms + 50 ms taker delay) is realistic — not fitted.
* Book-level fill model (no queue, no impact beyond our own consumption) is
  adequate for small FAK orders.

## 6. Results

See [validation.md](validation.md). On SYNTHETIC data the pipeline is
correct and safe (0 invariant violations, 0 reconciliation failures after
fixes, no orders during disconnects, deterministic, lookahead-free, kill
switch and auto-recovery behave as specified). Synthetic PnL is meaningless by
construction; robustness runs show strong sensitivity to latency; the
walk-forward calibrator did not earn enabling.

## 7. Bugs found and fixed (honest list)

Found by tests or validation, fixed in code (never by weakening tests):
worst-case edge above conservative edge; take-profit floor ignoring exit fee;
Decimal drift in pro-rata cost removal; order-book transient-cross
invalidation; LLM `required` mode failing open without a client; settlement
ledger bug (wrong token passed to the venue); drawdown kill switch tripping on
mark-to-market noise; plus a research correction (explicit wallet does not
prevent SDK wallet deployment).

## 8. Status matrix

| | Status |
|---|---|
| Safety layer (risk, hard caps, kill switch, watchdog, reconciliation, audit, secrets) | IMPLEMENTED, TESTED |
| Resolution rules | IMPLEMENTED, TESTED, **VERIFIED** on 11/11 real archived markets |
| Fee formula, geoblock format, `/time` format | **VERIFIED** (docs, SDK source, real responses) |
| Market data layer | IMPLEMENTED, TESTED (real payload fixtures); live streams **NOT VERIFIED** here |
| Strategy chain, paper exchange, execution engine, portfolio | IMPLEMENTED, TESTED |
| Replay / backtest / walk-forward / robustness | IMPLEMENTED, TESTED (synthetic data only) |
| Claude client | IMPLEMENTED, TESTED via SDK + mock transport; **NOT VERIFIED** with the real API |
| MCP server | IMPLEMENTED, TESTED (stdio); HTTP transport NOT IMPLEMENTED |
| Paper runner on live data | IMPLEMENTED; smoke-tested against a blocked network only; **NOT VERIFIED** |
| Live adapter + live bootstrap | IMPLEMENTED, TESTED with a fake SDK client; **NOT VERIFIED** |
| Docker | written; **NOT built/run** (no daemon in build environment) |
| On-chain redemption, order heartbeats, recorded-review replay | TODO |

## 9. Residual risks

1. Live adapter semantics unverified (order status strings, `after` parameter
   format, trade status lifecycle, positions API lag) — could halt or
   mis-reconcile on first contact; the fail-closed design should convert
   surprises into HALTs, not losses, but this is not proven.
2. UNKNOWN orders that timed out before an ack cannot be attributed
   automatically in live (venue does not know our intent ids) ⇒ manual halt.
3. Model risk: TWAP reproduction error and fat tails near expiry; edge on real
   markets is unknown.
4. Latency sensitivity (robustness run) — a slower host or network can turn
   the edge negative.
5. Private SDK method dependency (`_create`), mitigated by the exact pin and a
   test.
6. Regulatory / jurisdiction risk for the operator.

## 10. Next steps

1. Run `make record` / `make paper` from an eligible location with network
   access for ≥ 14 days; replay the recordings (`backtest --record-evidence`).
2. Fit latency and fill models to recorded paper sessions; re-run robustness.
3. Measure Claude's contribution on recorded sessions (reviews are persisted);
   implement recorded-review replay.
4. Verify the live adapter with a dry-run harness against the real API
   (read-only calls first: balances, open orders, trades), then a single
   minimum-size order under SMALL_LIVE caps with an operator watching.
5. Implement on-chain redemption and (if ever needed) order heartbeats.
6. Build and scan the Docker image in CI (bandit, pip-audit, secret scan).
