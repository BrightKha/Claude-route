# Progress log

Legend: **IMPLEMENTED** (code exists) · **TESTED** (automated tests pass) ·
**VERIFIED** (checked against real external systems/data) · **NOT VERIFIED** ·
**TODO**.

## Phase 1 — Research — DONE (2026-09-23)

See `docs/research.md`. Key outcomes:
- Official SDK is now `polymarket-client` 0.10.0 (unified); CLOB V2 since 2026-04-28; pUSD collateral.
- BTC 5m markets resolve on the **Chainlink 60 s TWAP** since 2026-08-14 (two rule changes in August).
- Resolution rule VERIFIED on 11/11 real resolved markets (fixtures saved).
- Fee formula VERIFIED (docs + SDK source). Geoblock endpoint format VERIFIED.
- France and 38 other countries are blocked by Polymarket (Help Center, 2026-08-14).
- SDK hazards found (implicit approve(max) + re-post, implicit wallet deployment, silent WS reconnects).
- **NOT VERIFIED**: any live call from this container (egress policy blocks polymarket.com).

## Phase 2 — Architecture — DONE

| Item | Status |
|---|---|
| CLAUDE.md, AGENTS.md, pyproject (uv), Makefile, pre-commit, .env.example | IMPLEMENTED |
| Domain model (types, clock, market, snapshot, decisions, orders) | IMPLEMENTED, TESTED (via risk/lifecycle tests) |
| Ports (discovery, market data, streaming, trading, account, resolution) | IMPLEMENTED |
| Config: RiskPolicy (hashed), AppConfig (strict YAML), EnvSettings (no secrets) | IMPLEMENTED, TESTED |
| configs/paper.yaml, live.example.yaml, risk_policy.example.yaml | IMPLEMENTED, TESTED (load) |

## Phase 3 — Safety first — DONE

| Component | Status |
|---|---|
| Risk Engine (40+ entry checks, exit checks, sizing, loss limits) | IMPLEMENTED, TESTED (83 tests incl. property test) |
| Hard caps + SMALL_LIVE caps (code-only) | IMPLEMENTED, TESTED |
| Bot state machine (strict transitions, LIVE needs LiveAuthorization) | IMPLEMENTED, TESTED |
| Kill switch (DB + sentinel file, manual reset with phrase) | IMPLEMENTED, TESTED |
| Watchdog (evaluator, async actor, loop-stall thread) | IMPLEMENTED, TESTED (evaluator/actor); loop-stall thread NOT TESTED yet |
| Reconciliation (positions, cash, external orders) | IMPLEMENTED, TESTED |
| Secret management (isolated loaders, Secret wrapper, redaction, excepthook) | IMPLEMENTED, TESTED |
| Hash-chained audit log | IMPLEMENTED, TESTED (tamper detection) |
| SQLite state store (write-ahead orders, idempotent fills, RO mode, MCP inbox) | IMPLEMENTED, TESTED |
| Promotion gates + live lock (15 checks) + compliance gate | IMPLEMENTED, TESTED |
| Secret scanner script + boundary tests | IMPLEMENTED, TESTED |

## Phase 4 — Data — DONE

| Component | Status |
|---|---|
| Market discovery (Gamma series events, slug fallback, rule validation on ingest) | IMPLEMENTED, TESTED (real Gamma fixtures) |
| Public REST (GET-only, bounded retries, no 4xx retry, 429 Retry-After), `/time` | IMPLEMENTED, TESTED (mock transport); `/time` format VERIFIED (integer seconds) |
| Resilient WebSocket (heartbeat, silence timeout, jittered backoff, explicit resubscribe) | IMPLEMENTED, TESTED (fake server) |
| CLOB order book (snapshot, deltas, tick change, echo verification, invalidation) | IMPLEMENTED, TESTED |
| Reference prices (RTDS Chainlink + Binance, outlier hold, EWMA vol, TWAP, price-to-beat) | IMPLEMENTED, TESTED; RTDS message format NOT VERIFIED live |
| Clock drift from exchange timestamps | IMPLEMENTED, TESTED |
| Session recorder (raw frames + bot events, synthetic flag) and strict replay reader | IMPLEMENTED, TESTED |
| REST fallback resync for books | IMPLEMENTED, TESTED (hub) |

## Phase 5 — BTC 5m chain — DONE

| Component | Status |
|---|---|
| Resolution rule registry (hash of verbatim rules text; twap60 v3 enabled, spot v1 registered-disabled, unknown => reject) | IMPLEMENTED, TESTED, VERIFIED (11/11 real resolved markets) |
| Features (versioned, no lookahead: future-data invariance test) | IMPLEMENTED, TESTED |
| Fair value: TWAP-aware Gaussian model with uncertainty band; optional calibrator that can only widen | IMPLEMENTED, TESTED; calibration against real outcomes NOT VERIFIED (needs recorded data) |
| Edge: book walk, fees, slippage buffer, exit cost, conservative + worst-case edge | IMPLEMENTED, TESTED |
| Exit engine (value, take-profit, invalidation, time, risk exits; hold when unpriceable) | IMPLEMENTED, TESTED |

Two real bugs were found by these tests and fixed in code (not by editing tests):
`worst_case_edge` could exceed the conservative edge; take-profit floor ignored the
exit fee. Other initial failures were wrong test scenarios (forgotten exit fee;
assumed exact Up/Down symmetry, which is not a model property) and were corrected
with added regression tests.

## Phase 6 — Paper execution — DONE

| Component | Status |
|---|---|
| Paper exchange: order matchable only at `t + latency + jitter + taker_delay`, matched against the book observed *then* | IMPLEMENTED, TESTED |
| FAK partial fills / FOK all-or-nothing; GTC/GTD refused | IMPLEMENTED, TESTED |
| Book walk (slippage) within limit; per-level official taker fees rounded up | IMPLEMENTED, TESTED |
| Own fills hide displayed liquidity for `liquidity_replenish_ms` | IMPLEMENTED, TESTED |
| Balance (incl. worst-case fee) and share checks; independent exchange ledger; settlement | IMPLEMENTED, TESTED |
| Execution engine: write-ahead, one submission, timeout/transport error/ambiguous ack => UNKNOWN (never resubmitted), no new orders while UNKNOWN, restart => UNKNOWN, venue-history resolution, operator resolution | IMPLEMENTED, TESTED |
| Fill idempotency; violations for unknown-order fill, fill beyond limit, overfill, terminal/fill mismatch | IMPLEMENTED, TESTED |
| Portfolio: fees in cost basis, marking at best bid, day roll, loss streak, settlement | IMPLEMENTED, TESTED (incl. cash-conservation property test) |
| End-to-end decision → engine → paper exchange → portfolio → reconciliation | TESTED (integration) |

Real bug found: pro-rata cost removal on partial sells used unquantized Decimal
division, so `cash + open cost − realized PnL` drifted by ~1e-25 USD. Fixed by
quantizing to 1e-10 USD (and removing the full basis on a full exit).

Limitations of the paper model (documented, deliberate): no queue position (we
never rest orders), no market impact beyond our own consumption, hidden liquidity
not modelled, the latency distribution is uniform and not fitted to real data.

Test count at end of Phase 6: **366 passing** (ruff, mypy --strict clean).

## Phase 7 — Claude & MCP — DONE (runtime wiring in the next step)

| Component | Status |
|---|---|
| `LLMReview` schema (APPROVE/REJECT/NO_OP, override, confidence, thesis, risks, invalidators, exit conditions, max holding) — extra fields refused | IMPLEMENTED, TESTED |
| Whitelisted `ReviewContext` (numbers only; no wallet/balance/keys/config) | IMPLEMENTED, TESTED |
| Override can only lower the probability band and edge; size never touched | IMPLEMENTED, TESTED (property test) |
| Budget: calls/min, calls/hour, spend/day with worst-case pre-reservation, per-market debounce, restart-safe seed | IMPLEMENTED, TESTED |
| Claude client (anthropic 1.8.0): structured outputs, effort, server-side fallback `default`, refusal/truncation/schema errors => no approval, no SDK retries, cost from usage, prompt secret guard | IMPLEMENTED, TESTED against the real SDK with a mock HTTP transport; **NOT VERIFIED** against the live API (no calls made from this environment) |
| Reviewer policy (off/advisory/required; approval TTL; price-move invalidation; reject cache; persisted + audited) | IMPLEMENTED, TESTED |
| MCP server (mcp 2.2.0, stdio): 14 read tools + `request_trade`/`request_close` proposals; read-only DB (`mode=ro` + app guard); refuses to start with secrets in env; outputs redacted; no raw order/withdraw/config/kill-switch tools | IMPLEMENTED, TESTED |
| HTTP transport for MCP | NOT IMPLEMENTED (refused at startup; stdio only) |
| Runtime processing of MCP proposals | TODO (runtime step) |

## Runtime, replay, CLI, live adapter — DONE (live NOT VERIFIED)

| Component | Status |
|---|---|
| `TradingCore`: one deterministic step shared by paper/replay/live (fills → settle → mark → loss limits → UNKNOWN resolution → reconciliation → lifecycle → exits → entries → MCP proposals → status) | IMPLEMENTED, TESTED |
| Kill switch on execution/portfolio invariant violations and loss-limit breaches | IMPLEMENTED, TESTED (replay) |
| Reconciliation each interval (never while an order is open), escalation after 2 consecutive mismatches | IMPLEMENTED, TESTED |
| Lifecycle: SYNCING→PAPER after reconciliation + no watchdog blockers; HALTED→SYNCING auto-recovery (rate-limited) | IMPLEMENTED, TESTED (disconnect chaos replay) |
| Claude review off the decision path (background task, verdict cache); `required` mode without a client blocks | IMPLEMENTED, TESTED |
| MCP proposals processed by the core: expiry, unknown market/kind rejected, trade needs a passing deterministic candidate, close priced as a risk exit | IMPLEMENTED, TESTED (rejection paths); accepted-trade path TESTED only indirectly |
| Replay engine: ticks before each message, paper matching before applying it, watchdog each tick | IMPLEMENTED, TESTED (determinism; no-lookahead by truncation) |
| SYNTHETIC session generator (real message formats, flagged `synthetic: true`) | IMPLEMENTED, TESTED |
| CLI: synth, replay, backtest (+robustness, +evidence), walk-forward, paper, record, status, kill-switch, promotion, live-readiness, mcp-server, `--mode paper/replay/live` | IMPLEMENTED, TESTED (live stays locked) |
| Live-data paper runner (WS + Gamma + REST resync + recorder + watchdog + loop-stall thread, graceful SIGTERM) | IMPLEMENTED; smoke-tested here only against a blocked network (reconnect/backoff/shutdown OK); **NOT VERIFIED** on real Polymarket data |
| Live venue adapter (SDK whitelist, `_create` to avoid wallet deployment, sign+post only, FAK/FOK only) | IMPLEMENTED, TESTED against a fake SDK client built from the SDK's models; **NOT VERIFIED** against the real exchange |
| Live bootstrap: flat dedicated wallet required, runtime live-lock re-evaluation before minting LiveAuthorization | IMPLEMENTED; **NOT VERIFIED** |
| Research: backtest report (PnL gross/net, CI, expectancy, hit rate, fill ratio, slippage vs planned VWAP, fees, drawdown, Brier/log-loss vs market-implied, calibration table, PnL by edge/TTE/vol/hour/exit/source), robustness perturbations, walk-forward calibrator | IMPLEMENTED, TESTED (metrics unit tests; end-to-end on SYNTHETIC data only) |

Bugs found in this step and fixed in code:
* **Order book** (real bug, market-data layer): a multi-level `price_change`
  event could pass through a transiently crossed state and wrongly invalidate
  the book; crossing is now checked once per event (regression tests added).
* **Fail-open gap**: with `llm.mode=required` and no usable Claude client the
  assembly created no reviewer, which the core treated as "LLM off" (trading
  allowed). A reviewer is now always built in paper/live; it answers
  "not reviewed", which blocks trading in `required` mode (test added).
* **Research correction**: explicit `wallet` does not prevent the SDK from
  deploying a Deposit Wallet (docs/research.md §1.1 corrected; adapter uses
  `_create`).

NOT AVAILABLE / TODO:
* "With vs without Claude" comparison: Claude is not called in replay and
  recorded reviews are not replayed yet.
* Automatic on-chain redemption of resolved positions (live): not implemented;
  settled winnings are tracked as `pending_redemption` until redeemed manually.
* MCP HTTP transport: not implemented (stdio only).
* Order heartbeats / resting orders: not implemented (FAK-only policy).

## Next

Phase 8 (validation report on synthetic + any recorded data), documentation
(README, architecture, security, trading, risk, deployment, backtesting,
incident response), Docker, Phase 9 final report.
