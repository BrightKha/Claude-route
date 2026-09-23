# Risk policy, hard caps, kill switch, promotion and live lock

## Layers

1. **Hard caps** (`risk/hard_caps.py`, code only): absolute ceilings/floors.
   A configured value beyond a cap is clamped (paper) or makes the live lock
   fail (live). Stage `SMALL_LIVE` adds tighter caps.
2. **Risk policy** (`configs/risk_policy.example.yaml` → `RiskPolicy`): the
   operator's limits, hashed (`policy_hash`). Every decision records the hash;
   the live confirmation and promotion approvals are bound to it.
3. **Risk Engine** (`risk/engine.py`): pure, deterministic, 40+ named checks
   on every entry and every exit. Allowed only if **every** check passes.

## Policy fields (paper example values)

| Field | Value | Meaning |
|---|---|---|
| `max_position_usd` | 20 | max cost basis per outcome token |
| `max_total_exposure_usd` | 60 | open cost bases + in-flight buys |
| `max_order_size_usd` | 20 | single order notional |
| `max_open_positions` / `max_positions_per_market` | 2 / 1 | never both sides of a window |
| `max_daily_loss_usd` / `max_daily_loss_pct` | 30 / 10 % | breach ⇒ KILL_SWITCH |
| `max_drawdown_pct` | 20 % | breach ⇒ KILL_SWITCH |
| `max_consecutive_losses` | 6 | entries blocked |
| `max_spread` / `max_slippage` | 0.04 / 0.02 | market quality |
| `min_liquidity_usd` | 25 | depth up to the limit price |
| `min_entry_price` / `max_entry_price` | 0.05 / 0.95 | no lottery tickets, no near-certainties |
| `min_conservative_edge` | 0.03 | after fees, slippage, uncertainty, exit cost |
| `max_uncertainty` | 0.25 | width of the probability band |
| `max_data_age_ms` / `max_reference_age_ms` | 2000 / 5000 | freshness |
| `max_clock_drift_ms` | 1000 | local vs exchange clock |
| `min_time_to_expiry_s` / `min_time_since_start_s` | 45 / 5 | timing |
| `require_reconciled_within_s` | 120 | last reconciliation must be recent and OK |
| `max_trades_per_minute/hour/day` | 2 / 20 / 120 | rate limits |
| `cooldown_same_market_s` / `cooldown_after_loss_s` | 30 / 60 | duplicates, tilt |
| `allow_exits_when_halted` / `exits_require_fresh_book` | true / true | exits |
| `max_order_age_seconds` | 30 | reserved (no resting orders under the FAK policy) |

Hard caps (examples): position ≤ 100, exposure ≤ 300, order ≤ 100, daily loss
≤ 100 / 20 %, drawdown ≤ 30 %, spread ≤ 0.10, entry price ∈ [0.03, 0.97],
min edge ≥ 0.01, time to expiry ≥ 20 s, cooldown ≥ 5 s. `SMALL_LIVE`: position
≤ 10, exposure ≤ 30, order ≤ 10, daily loss ≤ 20, trades/hour ≤ 12.
The example policy (position 20) therefore **does not qualify** for SMALL_LIVE
as is — the live lock will say so.

## Loss limits use conservative "risk equity"

`risk equity = cash + Σ min(best-bid value, cost basis)`. Unrealized losses
count in full; unrealized gains never raise the high-water mark until
realized. (Marking at bid alone let a transient near-expiry spike inflate the
peak and trip the drawdown limit on noise — found during validation.)

## Kill switch

Engaged by: loss-limit breach, execution/portfolio invariant violation,
watchdog `KILL` anomalies, operator (`make kill-switch`). Persisted in the DB
**and** a `KILL_SWITCH` sentinel file (either one engages it). Effects: no
entries; exits only at risk-exit prices if `on_kill_switch = exit_if_priced`.
Reset: `kill-switch reset --operator NAME --note "..." --confirm
I-HAVE-INVESTIGATED-AND-ACCEPT-RESET` ⇒ state DISABLED; a restart is required.

## Watchdog

Recoverable (auto-resume after resync, rate-limited): market/reference stream
down or silent, clock drift unknown, reconciliation stale. Manual: event-loop
stall, clock drift beyond limit, reconciliation mismatch, UNKNOWN orders,
unhandled exceptions. A separate thread detects a blocked event loop.

## Promotion pipeline (`promotion/gates.py`)

RESEARCH → BACKTEST (≥ 200 non-synthetic trades) → OUT_OF_SAMPLE (≥ 150 trades,
bootstrap PnL CI lower bound > 0, Brier skill vs market > 0) → PAPER (≥ 14 days,
≥ 150 trades, no open incidents, zero reconciliation failures) → SMALL_LIVE
(tests, security, kill-switch drill, reconciliation drill evidence + operator
approval) → LIVE (≥ 30 days / 200 trades at SMALL_LIVE + approval).
**Synthetic evidence never counts.** Approvals require the exact phrase
`APPROVE-STAGE-<STAGE>-<strategy version>-<policy hash prefix>`.

## Live lock (`promotion/live_lock.py`)

All must pass, re-evaluated at runtime before a `LiveAuthorization` exists:
`TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true`, `LIVE_CONFIRMATION` equal to
`I-ACCEPT-LIVE-RISK-<policy hash[:16]>`, config mode live, no clamping, strategy
enabled, promotion approved ≥ required stage and bound to the policy hash,
compliance (attested eligible jurisdiction + geoblock endpoint not blocked),
kill switch not engaged, flat dedicated wallet reconciled at startup, healthy
market data, credentials present, live extra installed, LLM policy coherent
(no trading without review when review is enabled).
