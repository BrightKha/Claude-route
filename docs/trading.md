# Trading logic — BTC Up or Down 5m

All external facts here are traceable to [research.md](research.md).

## Market and resolution

* One market per 5-minute window, slug `btc-updown-5m-{start_unix}`, two
  outcome tokens `Up` / `Down`, tick 0.01, min order 5 shares, taker-only fee
  `fee = shares × 0.07 × p(1−p)` (rounded up to 1e-5 by us, conservatively).
* Since 2026-08-14 (rule `btc_5m_twap60_v3`): **Up iff the Chainlink BTC/USD
  60-second TWAP at window end ≥ the same TWAP at window start** (the "price to
  beat"). Verified on 11/11 archived markets.
* A market is traded only if its verbatim description, resolution source and
  crypto config match the enabled rule (sha256). Unknown/changed text ⇒ not
  tradable. After resolution, the official outcome is checked against the rule
  (`finalPrice ≥ priceToBeat`); an inconsistency halts the bot.

## Fair value (`strategies/btc_5m/fair_value.py`)

Driftless arithmetic Brownian motion on the Chainlink spot with volatility
`S·σ` (EWMA of 1-second log returns). With `τ` seconds left, lookback `L = 60`:

* `τ ≥ L`: `E[F] = S`, `Var[F] = (Sσ)²(τ − 2L/3)`
* `τ < L`: `E[F] = ((L−τ)/L)·A + (τ/L)·S`, `Var[F] = (Sσ)² τ³/(3L²)`
  (`A` = observed spot average over the final window so far)
* plus a model-error variance for TWAP reproduction and feed basis.

`P(Up) = Φ((E[F] − K)/sd)`, clipped to [0.001, 0.999]. The **uncertainty band**
(σ ± 30 %, mean ± model error) — not the point estimate — drives decisions. An
optional logistic calibrator (research output) can only widen the band and is
disabled by default; the walk-forward study did not justify enabling it.

## Edge (`signals/edge.py`)

For the outcome with lower bound `l`:
walk the asks through levels whose marginal all-in cost (price + fee + slippage
buffer + expected exit cost) stays below `l − min_edge`; `executable_price` is
that VWAP; `conservative_edge = l − effective_price − expected_exit_cost`;
`worst_case_edge` assumes everything fills at the deepest level. At most one
side per window. Candidates below `min_conservative_edge` or with too wide a
band never reach the Risk Engine.

## Claude review (`llm/`)

| Mode | Behaviour |
|---|---|
| `off` | deterministic only |
| `advisory` (paper default) | REJECT blocks; APPROVE (confidence ≥ 0.5) may tighten; no review (budget, error, below threshold) ⇒ trade only if `allow_trading_without_llm` |
| `required` (live expects this or `advisory` + `allow_trading_without_llm=false`) | no approval ⇒ no trade |

Reviews run off the decision path; an approval is valid for `approval_ttl_s`
and only while the executable price has not moved more than
`max_price_move_since_review`. Refusal / invalid output ⇒ treated as REJECT.
Exits never wait for Claude.

## Execution

* FAK only (FOK allowed by the paper exchange for tests). No resting orders:
  the Python SDK exposes no order heartbeat (research §1.1).
* BUY limit = approved max price floored to the tick; SELL limit = approved
  min price ceiled to the tick; buy size in collateral, sell size in shares.
* Write-ahead intent, one submission, timeout/transport error ⇒ UNKNOWN (never
  resubmitted) ⇒ venue query ⇒ HALTED if still unknown.

## Exit policy (`exits/engine.py`)

Selling at bid `b` nets `b − fee(b)`; holding is worth ≈ `f`.

* **Value exits** (edge negative, converged, take profit): minimum price
  `ceil_tick(f + fee(f) − tolerance)`; take-profit never nets below the lower
  bound.
* **Risk exits** (kill switch, halt, max holding time, invalidation in `always`
  mode, MCP close proposal): minimum price
  `floor_tick(max(l − risk_exit_discount, min_exit_price))` — never a dump.
* **Hold** when unpriceable (stale book, no estimate, account anomaly), in the
  final `no_exit_window_s` seconds, or below the minimum order size: the
  maximum loss of a binary position is its cost.

## Settlement and redemption

Paper/replay: the simulated venue pays 1 per winning share at resolution.
Live: winnings stay as conditional tokens until redeemed on-chain;
**automatic redemption is not implemented** — the bot tracks them as
`pending_redemption` and excludes them from reconciliation until they are
redeemed by the operator.
