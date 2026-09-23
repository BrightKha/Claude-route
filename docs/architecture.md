# Architecture

## Processes

```
┌──────────────────────────── bot process (paper / live) ────────────────────────────┐
│ ResilientWebSocket(CLOB market) ─┐                                                 │
│ ResilientWebSocket(RTDS)  ───────┼─► SessionRecorder (raw JSONL)                   │
│ GammaDiscovery / REST resync ────┘        │                                        │
│                                           ▼                                        │
│                                  MarketDataHub  (books, reference prices,          │
│                                   validated markets, clock drift, resolutions)     │
│                                           │ snapshot()                             │
│   decision loop (1 s) ──► TradingCore.step()                                       │
│                             ├─ execution events ─► ExecutionEngine ─► Portfolio    │
│                             ├─ settle / mark / loss limits ─► KillSwitch           │
│                             ├─ Reconciler (venue ledger vs portfolio)              │
│                             ├─ ExitEngine ─► RiskEngine.evaluate_exit ─► Execution │
│                             ├─ Features ─► FairValue ─► Edge ─► CandidateReviewer  │
│                             │     (Claude, background task, cache)                 │
│                             │  └► RiskEngine.evaluate_entry ─► ExecutionEngine     │
│                             ├─ ProposalInbox (MCP proposals) ─► same path          │
│                             └─ publish status ─► StateStore.runtime_status          │
│   Watchdog task (1 s) ─► HALT / cancel / kill switch        LoopStallDetector thread│
│   Prometheus exporter (optional, 127.0.0.1)                                        │
│ Trading venue: PaperExchange (paper/replay) | PolymarketLiveVenue (live, SDK)      │
└────────────────────────────────────────────────────────────────────────────────────┘
          state.sqlite (WAL)   audit.jsonl (hash chain)   KILL_SWITCH sentinel
                 ▲ read-only (mode=ro)            ▲ write: proposals only
┌────────────── MCP process (no secrets in env, refuses otherwise) ─────────────────┐
│ MCPServer(stdio): 14 read tools + request_trade / request_close → inbox.sqlite     │
└────────────────────────────────────────────────────────────────────────────────────┘
```

The MCP server is a **separate process** with no access to the trading venue,
the SDK, the Anthropic client or any secret. It opens the state database with
SQLite `mode=ro` (plus an application guard) and can only append proposals to
a separate inbox database, which the bot validates like any other signal.

## Decision step (`app/core.py`)

Every tick, in this order (identical in paper, replay and live):

1. **Poll execution events** and apply them idempotently (fill ids are unique);
   execution invariants (unknown-order fill, fill beyond limit, overfill,
   terminal/fill mismatch) ⇒ kill switch.
2. **Settle** resolved markets (official Gamma outcome, rule-consistency
   check), **mark** positions at best bid, **loss limits** on risk equity ⇒
   kill switch on breach.
3. **Resolve UNKNOWN orders** by querying the venue; still unknown past the
   timeout ⇒ HALTED (manual).
4. **Reconcile** every `interval_s` (never while an order is open); a
   mismatch blocks entries immediately, a confirmed one halts (manual).
5. **Lifecycle**: SYNCING → PAPER/LIVE once reconciled and the watchdog has no
   blockers; HALTED → SYNCING automatically only for recoverable anomalies and
   at most `max_auto_recoveries_per_hour`.
6. **Exits** (never wait for Claude), then **entries**, then **MCP proposals**.
7. **Publish** status documents for the CLI and the MCP server.

## Lifecycle

```
DISABLED → INITIALIZING → SYNCING → PAPER | LIVE
PAPER/LIVE → HALTED (data outage, mismatch, exception) → SYNCING (auto if recoverable)
any → KILL_SWITCH (loss limit, invariant breach, operator) → DISABLED (manual reset only)
any → DEAD
```

Entering LIVE requires a `LiveAuthorization`, which only
`promotion/live_lock.py` can mint (enforced by a static test).

## Market data integrity

* A book is valid only after a full snapshot; it is invalidated on disconnect,
  crossed state (checked once per event), out-of-order update, malformed delta
  or a mismatch with the exchange's echoed best bid/ask.
* A market is tracked only if its verbatim resolution text, resolution source
  and crypto config hash to a registered, enabled rule version.
* The price to beat must be verified (official Gamma value agreeing with the
  Chainlink TWAP stream within tolerance) before any entry.
* Clock drift is estimated from exchange timestamps on the WebSocket.

## Persistence

| Store | Content | Writers |
|---|---|---|
| `state.sqlite` | orders (write-ahead), fills, risk decisions, candidates, LLM reviews, incidents, reconciliations, settlements, equity marks, promotion events, bot state, kill switch, runtime status | bot only |
| `inbox.sqlite` | MCP proposals | MCP server (append), bot (status) |
| `audit.jsonl` | hash-chained audit records (`verify_audit_log`) | bot |
| `recordings/<session>/` | raw messages + bot events (JSONL) for replay | bot |
| `KILL_SWITCH` | sentinel file (engaged if present) | bot, operator |

## Replay (`app/replay_engine.py`)

Messages are re-emitted in arrival order through the same hub. Before message
`e_k` (received at `t_k`): every decision tick `τ < t_k` runs first, then the
paper exchange matches orders due by `t_k` against the book *before* `e_k` is
applied, then `e_k` is applied. Future data therefore cannot influence past
decisions — this is tested by truncating a session and comparing decisions.
