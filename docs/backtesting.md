# Backtesting, replay and validation

## Principles

* **Same code as live.** Replay feeds recorded raw messages through the same
  hub, features, fair value, edge, risk, execution, exit and portfolio code.
* **No lookahead.** Decision ticks strictly before a message run before it is
  applied; the paper exchange matches orders due by then against the book as
  it was; the simulated clock only moves forward (backwards ⇒ error). A test
  replays a session truncated at `T` and checks that all decisions before `T`
  are identical.
* **Deterministic.** Seeded latency jitter; same session ⇒ same orders and PnL.
* **Research is separate.** Research code may import production code, never
  the reverse (static test). Promotion is manual.

## Paper exchange model (`adapters/paper_exchange.py`)

* Order matchable at `submit + latency + jitter + taker_delay`, against the
  book observed at that time — never at the mid.
* FAK: fills available levels within the limit, cancels the rest. FOK: all or
  nothing. GTC/GTD refused.
* Our own fills hide displayed size for `liquidity_replenish_ms` (no double
  counting of liquidity).
* Official taker fee per level, rounded up; optional `fee_multiplier` for
  stress tests. Balance check includes the worst-case fee.
* Not modelled (documented limitations): queue position (we never rest),
  market impact beyond our own consumption, hidden liquidity, latency fitted to
  real data.

## Data sources

| Source | How | Counts as evidence |
|---|---|---|
| Recorded live sessions (`data/recordings/…`) | `make record` / `make paper` | yes |
| SYNTHETIC sessions | `make synth` | **never** (flag in `session.json`) |

The SYNTHETIC generator (`research/synthetic.py`) emits real message formats
(Gamma markets with the real rule text, CLOB `book`/`price_change` with
echoes, RTDS spot/TWAP/secondary, connection events, resolutions) from a
simulated BTC path and a simulated market maker whose mispricing is an
explicit assumption. It exercises the pipeline; its PnL means nothing.

## Metrics (`research/backtest.py`)

PnL gross/net, fees, expectancy, hit rate, bootstrap 95 % CI of net PnL, max
drawdown (mark-to-market), fill ratio, slippage of fills vs planned VWAP,
Brier and log loss of entries vs the market-implied price (Brier skill),
calibration table, PnL by edge bucket / time-to-expiry / volatility regime /
UTC hour / exit kind / signal source, safety summary (final state, kill switch,
violations, reconciliation failures, incidents).

"With vs without Claude" is **not available** in replay: Claude is not called
and recorded reviews are not replayed yet. Paper sessions record every review
(`llm_reviews` table) so the comparison can be built on recorded data.

## Robustness

`backtest --robustness` re-runs the session under: latency +300 ms, +1000 ms,
fees ×1.5, slippage buffer +1 c, min edge +2 c. A strategy whose PnL flips
sign under small perturbations must not be promoted.

## Walk-forward calibration (`research/walk_forward.py`)

Samples decision-time features and the baseline probability every 15 s per
running market (no lookahead), attaches official outcomes, trains a logistic
calibrator on expanding chronological folds and evaluates Brier/log loss out
of sample against the baseline and the book mid. The final calibrator artifact
is written with its dataset hash; enabling it is a manual decision.

## Commands

```bash
make synth                       # SYNTHETIC session (pipeline validation only)
python -m polymarket_bot.app replay   --input <session>
python -m polymarket_bot.app backtest --input <session> --report reports/bt --robustness
python -m polymarket_bot.app walk-forward --input <session> --report reports/wf
python -m polymarket_bot.app backtest --input <recorded session> --report … --record-evidence
```

Latest results: [validation.md](validation.md).
