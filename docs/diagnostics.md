# Decision-pipeline diagnostics ("why no trade?")

Observability only. Nothing described here feeds back into a decision: the
counters and diagnostics are written by the trading core and never read by it,
so they cannot change what the bot trades (the synthetic backtest numbers are
identical before and after they were added — see "Verification" below).

## How to use

```
make paper        # terminal 1: paper session on live public data
make diagnose     # terminal 2 (any time): read-only report from data/state.sqlite
make ptb-validate # add price-to-beat evidence from data/recordings (never changes the policy)
uv run python -m polymarket_bot.app --config configs/paper.yaml diagnose --json   # full JSON
```

`diagnose` opens the state database read-only and never writes (tested). The
paper process also logs, every `monitoring.pipeline_log_interval_s` (default
60 s, `config/app_config.py`), one INFO funnel line, one INFO line per running
market and an INFO verdict; `--log-level DEBUG` adds one line per decision
with its NO_TRADE reason. The same summary is written to the session recording
as a `pipeline_summary` bot event. Replays log it at DEBUG only, and
`make replay` prints the funnel in its JSON summary.

## The funnel (since the core started)

One **decision** = one running BTC 5m market × one decision step (1 s).

| counter | meaning |
|---|---|
| market updates | raw market-data messages (CLOB WS frames, RTDS frames, Gamma, REST books), **excluding** connection events and `PONG` heartbeats |
| decisions | running-window markets evaluated |
| features computed | snapshots turned into a feature vector (always, whatever the bot state) |
| fair value ok | fair-value estimates usable for trading |
| candidates / rejected / passing | two per decision (Up, Down); "passing" = cleared every deterministic edge filter |
| reached Risk Engine | entry candidates evaluated by the Risk Engine (after the Claude verdict lookup) |
| risk approved / rejected | Risk Engine outcome, with every rejection reason counted |
| paper orders / fills | orders accepted by the execution engine / fills applied to the portfolio (entry and exit) |
| exits submitted | exit orders |

Each stage that is zero comes with a "why" computed from the counters, and each
decision gets a **NO_TRADE reason** = the *first* blocking stage, in this
order: `data:` (first stale-snapshot reason) → `fair value:` → `edge (side):`
(first rejection of the best candidate) → `bot state:` (e.g. SYNCING, kill
switch) → `llm:` (review requested/pending/rejected) → `risk:` →
`execution …`. Reasons are counted with numbers masked (`#`) so the counters
stay bounded.

The report also lists: stale-snapshot reasons, fair-value failure reasons,
every candidate rejection, bot-state blocks, Claude verdict statuses and risk
rejections, then a one-line **verdict** ("INTEGRATION PROBLEM …", "DATA
PROBLEM …", "NO TRADE EXPECTED with this configuration …").

## Market selection diagnostics

For each running market: slug, market id, condition id, both token ids,
window start/end, elapsed and remaining time, accepting orders, per token best
bid/ask, spread, depth, book age and validity (with the invalidation reason),
Chainlink spot / 60 s TWAP / Binance values with their ages and dispersion,
the price to beat (value, source, verified), the official Gamma value, the RTDS
TWAP tick at the window start (exact value and offsets of the nearest ticks),
the resolution rule and outcome, stale reasons, the fair value and both
candidates with their rejections. `TRACKED MARKETS` shows every market the
rolling-slug discovery (`btc-updown-5m-<window start>`) is following and its
phase (upcoming / running / ended / resolved).

`FEEDS` shows messages per `source:kind`, applied CLOB events per type,
RTDS messages per `type|topic|symbol` (shows whether each topic arrives, and
under which symbol), stored ticks, clock drift and markets rejected by rule
validation.

`REFERENCE FEEDS` shows every input of the dispersion check with its proof:

```
reference values:      value, age, source, quote currency, topic, encoding of
                       full_accuracy_value on that topic, and the server's raw
                       full_accuracy_value / value strings with how they were decoded
normalized values:     USD per BTC (the Binance secondary is USDT; no conversion)
dispersion pairs:      spot/twap60, spot/secondary, twap60/secondary (bps)
dispersion final:      |ln(spot / secondary)| * 1e4 vs max_source_dispersion_bps
```

plus per-series updates, backfill (history) points and rejected ticks by reason
(`unit_mismatch`, `malformed`, `window`, `wrong_symbol`, `outlier`,
`out_of_order`), empty frames and server error envelopes.

`REFERENCE COUNTERS`: `reference_messages`, `spot_updates`, `twap_updates`,
`secondary_updates`; per decision `spot|twap|secondary` `valid` / `stale` /
`missing` (valid = present and age ≤ `risk.max_reference_age_ms`),
`dispersion_rejects`, `price_to_beat_verified` / `price_to_beat_unverified`.

`LIVENESS` keeps the signals apart: socket alive, protocol heartbeat, any
frame, order-book events (`book`/`price_change` applied to a tracked book),
price events (`last_trade_price`/`best_bid_ask`/`tick_size_change`) and live
reference ticks per series. **A heartbeat is not data**: the watchdog's
`market_stream_silent` reads book events only and `reference_stream_silent`
reads live Chainlink spot/TWAP ticks only (not the Binance cross-check, not
subscribe backfills); the anomaly detail lists every age.

`PRICE-TO-BEAT CHECKS`: when Gamma first publishes a market's official
`priceToBeat`, the hub records how long after the window end that happened and
the difference with the RTDS TWAP tick at the window start. Discovery keeps
re-querying an ended market until that value is published (at most 30 min).
This is the evidence needed before any change to the price-to-beat policy.

`PRICE_TO_BEAT_VALIDATION` (persisted in `state.sqlite`, table
`price_to_beat_observations`, one row per window, first observation wins):
`N_WINDOWS` (official value and RTDS tick at the window start both known),
`MATCH_COUNT` (|diff| ≤ `fair_value.stream_price_to_beat_max_diff_bps`,
0.01 bps), `MAX_ABS_DIFF_BPS`, `P95_ABS_DIFF_BPS`, `MEAN_ABS_DIFF_BPS`, plus
`MISSING_STREAM`, mismatching windows, the policy and the gate. The paper
runner adds observations live; `make ptb-validate` replays the Gamma + RTDS
messages of every real recording in `data/recordings` (synthetic sessions are
skipped, never evidence) and adds what it finds.

The in-window policy `fair_value.stream_price_to_beat_policy` is **`off` by
default** (NO_TRADE without the official value). `evidence_gated` uses the
stream value only while `N_WINDOWS ≥ stream_price_to_beat_min_windows` (the
schema refuses < 100) and `MATCH_COUNT == N_WINDOWS`. Any later mismatch is a
`price_to_beat_mismatch` incident; with the policy on it also closes the gate
and halts the bot for the operator. Enabling it is a config change by the
operator only (no MCP/LLM path), after reading the evidence.

`HALTS` (from the audit log, older logs included): halt time, from→to state,
cause, source component, category (feed / risk / execution / reconciliation /
runtime / …), exact condition (watchdog anomalies with every liveness age),
recovery start, recovery time and downtime.

## Audit log vs decisions

The hash-chained audit log (`data/audit.jsonl`) records **significant events
only**: `core_start`/`core_stop`, state changes, order submissions and order
state changes, fills, settlements, Claude reviews, kill switch, MCP proposals,
operator resolutions, and — for every halt — an explicit `halt` event
(`halt_id`, time, cause, component, category, condition), then
`recovery_started` and `recovered` carrying the same `halt_id` and the
original cause (an automatic recovery never hides why the bot stopped). A
fresh paper start writes 4 records (`core_start` + 3 state changes
DISABLED→INITIALIZING→SYNCING→PAPER); the file accumulates across runs in the
same data directory. NO_TRADE decisions are deliberately **not**
journaled (≈ 1 per market per second would bloat a tamper-evident log and its
fsyncs); they are counted by the pipeline counters, logged at DEBUG, summarised
in the recording, and `risk_decisions` rows exist for every candidate that
reached the Risk Engine. `diagnose` prints the audit record kinds and the
latest records.

## 2026-09-25 diagnosis: 0 paper trades in 20 minutes

Symptom: `make paper` healthy for ~20 min (PAPER, 1258 steps, streams
connected, reconciliation OK), no candidate, no risk decision, no order.

Root cause (**integration problem**, not strict thresholds): Gamma publishes
`eventMetadata.priceToBeat` only **after** a window has ended (docs/research.md
§5, observed live on consecutive windows). The core requires a verified price
to beat (official Gamma value, optionally cross-checked with RTDS), so during
every running window the snapshot is stale ("price to beat not verified"), the
fair value is not computed ("price to beat unknown/unverified") and every
candidate is rejected before the Risk Engine. With this rule, the bot can never
trade — whatever the thresholds. It fails closed as designed; nothing unsafe
happened.

Proposed corrections (status after the second session, below):

1. Price-to-beat source in-window: accept the RTDS `crypto_prices_twap_sixty`
   tick observed exactly at the window start (by definition the 60 s TWAP at
   the start, and equal to the previous window's settlement value) **only
   after** `PRICE-TO-BEAT CHECKS` show it matches Gamma's later official value
   (e.g. ≤ `price_to_beat_tolerance_bps` on ≥ 100 consecutive windows), and
   keep the post-window official cross-check as an alarm (mismatch ⇒ halt).
   → IMPLEMENTED as `stream_price_to_beat_policy` (**off**) + evidence gate;
   NOT enabled: 2 matching live windows so far, 100 required.
2. If the TWAP topic turns out not to arrive (see `FEEDS`) → not needed: the
   second session received all three topics.
3. Count only book/price messages (not `PONG`) as market-data activity for the
   watchdog → IMPLEMENTED (book events and live spot/TWAP ticks only).

## 2026-09-25 (second session): "source dispersion" on 954/965 decisions

Observed: 965 decisions, 452,601 market updates, 880,750 book events, fair
value ok 0, 1,930 candidates, 0 passing; NO_TRADE: 954 `data: source
dispersion`, 4 `reference stale`, 2 `Up book invalid`, 2 `websocket
disconnected`; spot 84166.00011090243, twap60 84163.63636998429, secondary
8.418607e-14, dispersion 414462.93 bps.

Root cause (**integration bug, ours**): the legacy RTDS `crypto_prices`
(Binance) topic sends `full_accuracy_value` as a plain decimal string
(`"84186.07000000"`), the Chainlink topics as an E18 integer. The parser
divided every `full_accuracy_value` by 1e18: 84186.07 / 1e18 = 8.418607e-14,
and |ln(84166.00011090243 / 8.418607e-14)|·1e4 = 414462.93 bps — both of the
session's numbers are reproduced exactly (test
`test_before_after_secondary_scale_on_the_observed_values`). With per-topic
decoding the same payload gives 84186.07 and a dispersion of 2.384 bps
(threshold 50). Evidence: docs/research.md §4.1.

Note: the dispersion reason came first in the stale list and masked the
price-to-beat reason (still present: `fair value ok = 0`). With the fix,
expect the first NO_TRADE reason to become `data: price to beat not verified`
again until the price-to-beat evidence reaches 100 windows **and** the
operator enables the policy — i.e. still no trade with the default
configuration. That is the intended fail-closed behaviour.

The two `PAPER → HALTED → SYNCING → PAPER` cycles: the session's own audit
log and `watchdog_halt` incidents hold the causes; `make diagnose` (HALTS)
now prints them (for older logs from the state-change reason and the
incident). The `2 websocket disconnected` / `Up book invalid` decisions point
to market-websocket reconnects (books invalidated and resynced, clock drift
re-measured) — to be confirmed from the HALTS section of that data directory.

Even with a valid price to beat, trades should be rare: an entry needs the
model's lower probability bound to exceed the ask by ≈ 6–7 cents (3 c minimum
conservative edge + taker fee ≈ 1.75 c at 0.5 + 0.5 c slippage buffer + expected
exit cost), with at least 45 s left and ≥ 60 s of volatility history.

## Verification

* Unit/integration tests: `tests/unit/test_pipeline_diagnostics.py`
  (normalisation, first-blocking-stage classification, zero explanations,
  verdicts, hub message/topic counts, price-to-beat evidence, discovery,
  reproduction of the live symptom with real payload shapes, read-only CLI).
* `tests/unit/test_reference_feeds.py` (per-topic units on verbatim captured
  frames, the before/after of the observed values, unit-mismatch rejection,
  Decimal exactness, symbols, server errors, backfill history, PolyBolt
  snapshot/update/sequence/reconnect, liveness separation, RTDS reconnect) and
  `tests/unit/test_halts_and_ptb_validation.py` (halt journal and history,
  validation stats, OFF-by-default gate, ≥ 100 windows, mismatch halt,
  `ptb-validate` on recordings).
* Synthetic backtest (`make synth && make backtest`): same trades and PnL as
  the reference in docs/validation.md (all robustness rows identical).
