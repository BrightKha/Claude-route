# Validation (Phase 8) — 2026-09-23

> **Everything below ran on SYNTHETIC data.** The development environment has
> no network access to Polymarket, so no real market session could be
> recorded or replayed. These runs validate the *pipeline* (correctness,
> safety behaviour, determinism, metrics). **They say nothing about whether
> the strategy makes money on Polymarket.** Promotion gates ignore them.

Artifacts: [validation/synthetic-2026-09-23/](validation/synthetic-2026-09-23/)
(`report.md`, `robustness.md`, `chaos-report.md`, `walk_forward.json`,
`calibrator.SYNTHETIC-do-not-enable.json`).

## Automated test suite

`make check`: ruff (lint + format), mypy `--strict` (93 modules), bandit
(documented skips B101/B311), secret scan, **pytest: 450+ tests passing**
(unit, property-based, integration, replay, execution, security).

## One simulated day (288 windows, seed 7, 383,916 messages)

| | value |
|---|---|
| trades (closed) | 27 |
| net / gross PnL | +64.63 / +81.57 USD (fees 16.94) |
| hit rate | 70.4 % |
| net PnL 95 % bootstrap CI | (−9.02, +137.95) — includes zero |
| max drawdown (mark-to-market) | 47.68 USD (20.7 %) |
| entry fill ratio (FAK after latency) | 22.5 % |
| execution invariant violations | 0 |
| reconciliation failures | 0 |
| final state | HALTED — market data silent after the last window (expected) |

The positive PnL comes from the generator's **assumed** market-maker
mispricing (`mm_noise`); it is not evidence of an edge.

### Robustness

| perturbation | trades | net PnL |
|---|---|---|
| baseline | 27 | +64.63 |
| latency +300 ms | 27 | +64.63 |
| latency +1000 ms | 6 | −20.34 |
| fees ×1.5 | 27 | +57.99 |
| slippage buffer +1 c | 23 | +129.32 |
| min edge +2 c | 23 | +23.88 (kill switch on daily loss 10.15 % ≥ 10 %) |

Reading: results are very sensitive to latency (one more second of delay turns
the simulated edge negative) and to thresholds — typical of a short-horizon
strategy and a warning for real trading.

### Walk-forward calibration (5,472 samples, 4 expanding folds)

The logistic calibrator improved out-of-sample Brier score in **1 of 4 folds**
⇒ **not enabled**. Over all samples the baseline model's Brier skill versus
the book mid is ≈ 0 (−6 % … +0.5 % by fold); the positive skill measured on
executed entries (0.35) is a selection effect of the synthetic noise.

## Chaos run (48 windows, disconnects in ~50 % of windows, seed 21)

| | value |
|---|---|
| disconnect intervals | 22 |
| watchdog halts / automatic recoveries | 7 / 7 |
| orders placed during or < 1 s after a disconnect | **0** (of 41) |
| execution invariant violations / reconciliation failures | 0 / 0 |
| kill switch | engaged on a real daily loss of 10.4 % after 3 losing trades (limit 10 %) |

## Real bugs found by validation (all fixed in code, with regression tests)

1. **Order book** — multi-level `price_change` events could pass through a
   transiently crossed state and invalidate a healthy book (so every order was
   cancelled for "no valid book"). Crossing is now checked once per event.
2. **Settlement ledger** — `_settle` passed a token id to a method expecting an
   outcome label and handed the venue an object instead of a token id: the
   losing position stayed in the paper venue ledger (reconciliation caught it
   and halted; the winner could also be misidentified when "Up" won). Fixed;
   `settle_venue` is now typed; the regression test fails on the old code.
3. **Drawdown false positive** — marking at best bid let a near-expiry bid
   spike (0.09 → 0.99 on ~46 cheap shares) inflate the high-water mark, and the
   drawdown kill switch fired although the position could lose at most its
   cost (~5 % of capital). Loss limits now use conservative risk equity.

Earlier phases also fixed real bugs found by tests (worst-case edge could exceed
the conservative edge; take-profit floor ignored the exit fee; Decimal drift in
pro-rata cost removal; LLM `required` mode failing open without a client).

## Not validated

* Real Polymarket market data (no network access from the build environment).
* The live venue adapter against the real exchange (never placed an order).
* The Claude API with a real key (tested through the SDK with a mock transport).
* Docker image build/run (no Docker daemon in the build environment).
