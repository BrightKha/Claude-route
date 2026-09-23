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

## Next

Phase 4 (data), Phase 5 (BTC 5m chain), Phase 6 (paper execution), Phase 7 (Claude/MCP), Phase 8/9.
