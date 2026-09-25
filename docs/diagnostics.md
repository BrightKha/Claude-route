# Decision-pipeline diagnostics ("why no trade?")

Observability only. Nothing described here feeds back into a decision: the
counters and diagnostics are written by the trading core and never read by it,
so they cannot change what the bot trades (the synthetic backtest numbers are
identical before and after they were added — see "Verification" below).

## How to use

```
make paper        # terminal 1: paper session on live public data
make diagnose     # terminal 2 (any time): read-only report from data/state.sqlite
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

`FEEDS` shows messages per `source:kind` (so heartbeats can be told apart from
book data: the websocket health "last message" is refreshed by `PONG` too),
applied / ignored book events, RTDS messages per `type|topic|symbol` (shows
whether the TWAP topic arrives, and under which symbol), stored ticks, clock
drift and markets rejected by rule validation.

`PRICE-TO-BEAT CHECKS`: when Gamma first publishes a market's official
`priceToBeat`, the hub records how long after the window end that happened and
the difference with the RTDS TWAP tick at the window start. Discovery keeps
re-querying an ended market until that value is published (at most 30 min).
This is the evidence needed before any change to the price-to-beat policy.

## Audit log vs decisions

The hash-chained audit log (`data/audit.jsonl`) records **significant events
only**: `core_start`/`core_stop`, state changes, order submissions and order
state changes, fills, settlements, Claude reviews, kill switch, MCP proposals,
operator resolutions. A fresh paper start writes 4 records (`core_start` + 3
state changes DISABLED→INITIALIZING→SYNCING→PAPER); the file accumulates across
runs in the same data directory. NO_TRADE decisions are deliberately **not**
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

Proposed corrections (NOT implemented; each needs evidence and an explicit
research/policy decision, see docs/research.md):

1. Price-to-beat source in-window: accept the RTDS `crypto_prices_twap_sixty`
   tick observed exactly at the window start (by definition the 60 s TWAP at
   the start, and equal to the previous window's settlement value) **only
   after** `PRICE-TO-BEAT CHECKS` show it matches Gamma's later official value
   (e.g. ≤ `price_to_beat_tolerance_bps` on ≥ 100 consecutive windows), and
   keep the post-window official cross-check as an alarm (mismatch ⇒ halt).
   Implemented as a new, versioned policy flag (default off), with tests.
2. If the TWAP topic turns out not to arrive (see `FEEDS`): align the RTDS
   subscription with the official SDK (no server-side `filters`), after
   recording evidence.
3. Count only book/price messages (not `PONG`) as market-data activity for the
   watchdog's silence detector (book staleness already blocks trading, so this
   is a monitoring accuracy fix, not a safety gap).

Even with a valid price to beat, trades should be rare: an entry needs the
model's lower probability bound to exceed the ask by ≈ 6–7 cents (3 c minimum
conservative edge + taker fee ≈ 1.75 c at 0.5 + 0.5 c slippage buffer + expected
exit cost), with at least 45 s left and ≥ 60 s of volatility history.

## Verification

* Unit/integration tests: `tests/unit/test_pipeline_diagnostics.py`
  (normalisation, first-blocking-stage classification, zero explanations,
  verdicts, hub message/topic counts, price-to-beat evidence, discovery,
  reproduction of the live symptom with real payload shapes, read-only CLI).
* Synthetic backtest (`make synth && make backtest`): same trades and PnL as
  before the diagnostics were added (docs/validation.md).
