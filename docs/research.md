# Phase 1 — Technical research (2026-09-23)

This document records what was verified, **how** it was verified, and what
remains unverified. Every design decision in the codebase that depends on an
external fact should point back here.

Legend: **VERIFIED** = checked against an official primary source (official
docs, official SDK source code, or real API payloads). **OBSERVED** = seen in
real API payloads but not documented. **UNVERIFIED** = could not be checked.

---

## 0. Research environment and its limits

| Item | Finding |
|---|---|
| OS / tools | Ubuntu 24.04, `uv` 0.8.17, CPython 3.10–3.13 available (3.11 default). |
| Egress policy | The container's egress proxy **blocks all `*.polymarket.com` hosts** (HTTP 403 at CONNECT), as well as Binance/Coinbase APIs and `github.com`. PyPI and `raw.githubusercontent.com` are reachable. |
| Consequence | **No live Polymarket call can be made from this environment.** Official docs and real API payloads were retrieved through an external scraping service (Firecrawl) and PyPI wheels were downloaded and read directly. Real payloads were saved under `tests/fixtures/` and are used by tests. |
| Live integration status | **UNVERIFIED end-to-end.** The live adapter is written against the SDK's source and is type-checked against the installed SDK, but it has never talked to Polymarket. It must be validated by the operator (see `docs/deployment.md`). |

---

## 1. Polymarket SDKs — which one is current?

| Package | Version (PyPI) | Status | Source |
|---|---|---|---|
| `polymarket-client` (import `polymarket`) | **0.10.0** (2026-09-10) | **Official unified SDK**, "Development Status :: 4 - Beta", Python ≥ 3.11. Recommended for new projects. | PyPI metadata; `py-clob-client-v2` README note; docs `llms.txt` ("Python SDK: Get started with the unified Polymarket Python SDK") |
| `py-clob-client-v2` | 1.1.0 (2026-07-17) | CLOB V2 trading client (trading only). | PyPI; docs `/v2-migration` |
| `py-clob-client` | 0.34.6 (2026-02-19) | **Legacy V1 — no longer works against production** since the CLOB V2 cutover (2026-04-28). | docs `/v2-migration` |
| `Polymarket/agent-skills` (GitHub) | — | **Outdated**: still references `py-clob-client` and USDC.e collateral. Used only for concepts (order types, WS event names), always cross-checked. | raw.githubusercontent.com |

**Decision:** use `polymarket-client==0.10.0` (pinned exactly, because 0.x minor
releases may break) **only inside the live execution adapter**, installed via
the optional `live` extra. Everything else talks to internal interfaces.

### 1.1 Hazards found by reading the SDK source (VERIFIED in code)

1. **Implicit on-chain approvals.** `AsyncSecureClient.place_limit_order()` and
   `place_market_order()` call `post_order_with_allowance_recovery()`: when the
   order is rejected for balance/allowance, the SDK **sends an on-chain
   `approve(max)` transaction and re-posts the same order**.
   → Our adapter never calls `place_*`; it signs with `create_limit_order()` /
   `create_market_order()` and submits with `post_order()`, which has no
   recovery behaviour. Allowance problems surface as rejections → HALT.
2. **Implicit wallet deployment.** `AsyncSecureClient.create()` calls
   `_ensure_wallet_ready()`, which **deploys a Deposit Wallet through the
   relayer** when the wallet is a Deposit Wallet that is not yet deployed.
   **Correction (Phase 7, re-reading `_ensure_wallet_ready` /
   `_deploy_default_deposit_wallet`):** passing an explicit `wallet` does *not*
   prevent this when that address equals the signer's default Deposit Wallet —
   the SDK then deploys it. Only a *different*, non-deployed address raises.
   → The adapter requires an explicit wallet address
   (`POLYMARKET_WALLET_ADDRESS`) **and** builds the client with
   `AsyncSecureClient._create(...)`, which performs the same key/credential
   setup without `_ensure_wallet_ready()`. A non-deployed wallet then surfaces
   as order rejections (→ HALT), never as an on-chain deployment. This relies
   on a private SDK method: the pin is exact (`==0.10.0`) and a test fails if
   the method's signature changes.
3. **Dangerous capabilities on the same object.** The secure client exposes
   `transfer_erc20`, `approve_erc20`, `withdraw_from_perps`,
   `execute_transaction`, `split/merge/redeem_positions`, etc.
   → The SDK object is private to `adapters/polymarket_live.py`; only a
   whitelisted set of operations is wrapped. A security test asserts that no
   module outside that adapter imports `polymarket`.
4. **Silent WebSocket reconnection.** The SDK stream managers reconnect and
   resubscribe internally without telling the consumer that a gap occurred.
   → Market data uses our own WebSocket client with explicit connection state,
   book invalidation on disconnect and resync before trading resumes.
5. **Order heartbeats not exposed in Python.** The docs page "Manage Orders →
   Order Heartbeats" says *"Python: Content coming soon"*. The REST endpoint is
   `POST /v1/heartbeats` (10 s timeout, cancel check every 5 s).
   → Live policy forbids resting orders (GTC/GTD) until a heartbeat is
   implemented and verified. Entries and exits use **FAK** only.

---

## 2. CLOB V2 facts (VERIFIED: docs `/v2-migration`, changelog, SDK source)

- CLOB V2 live since **2026-04-28**; V1-signed orders rejected; all V1 open
  orders were wiped.
- Collateral is **pUSD** (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`), not
  USDC.e. Exchange `0xE111180000d2663C0091e4f400237545B87B996B`, neg-risk
  exchange `0xe2222d279d744050d28e00520010520000310F59` (SDK `environments.py`
  and docs agree). EIP-712 exchange domain version `"2"`.
- Order types: `GTC`, `GTD`, `FOK`, `FAK`. Market orders are `FAK`/`FOK`: BUY
  takes an amount in collateral, SELL takes shares. `post_only` only with
  GTC/GTD.
- GTD: SDK docstring requires expiration **≥ 3 minutes in the future**. On a
  5-minute market this makes GTD practically unusable → not used.
- Allowed tick sizes (SDK `_ALLOWED_TICK_SIZES`): `0.1, 0.01, 0.005, 0.0025,
  0.001, 0.0001`. BTC 5m markets: `0.01` normally, **`0.001` observed** on a
  market near 0/1 (tick changes are pushed as `tick_size_change`).
- Minimum order size on BTC 5m: **5 shares** (`orderMinSize`, `mos`).
- `POST /order` response: `success`, `errorMsg`, `orderID`, `status`
  (`live|matched|delayed`), `makingAmount`, `takingAmount`, `tradeIDs`. Since
  2026-07-24 FAK/FOK return `tradeIDs` and no transaction hashes.
- Trade status lifecycle: `MATCHED → MINED → CONFIRMED`, with `RETRYING` and
  terminal `FAILED`.
- Matching-engine restarts: HTTP **425** on order endpoints during restart,
  then **2 minutes of post-only mode** (HTTP 503, code `post_only_mode`).
  Cancel-only mode also exists (503). Docs: do **not** blindly retry.
- Account "closed-only mode" exists (`get_closed_only_mode()`).
- Crypto markets have a **taker delay of 50 ms** (since 2026-08-17; 250 ms
  before) — modelled in the paper exchange.
- Rate limits (2026-06-01): `POST /order` 120 000 / 10 min sustained. The bot's
  own limits are orders of magnitude lower.
- REST `GET /book`: bids sorted **ascending**, asks **descending** (best level
  is the *last* element) — the parser sorts explicitly and never relies on
  order. Timestamps are epoch **milliseconds** as strings.

## 3. Fees (VERIFIED: docs `/trading/fees`, SDK `adjust_buy_amount_for_fees`)

```
fee_usdc = shares × rate × (p × (1 − p)) ^ exponent      (takers only)
```

- Crypto: `rate = 0.07`, `exponent = 1`, taker-only, 20 % maker rebate.
  Peak $1.75 per 100 shares at p = 0.50. Rounded to 5 decimals.
- Per-market parameters: Gamma `feeSchedule {exponent, rate, takerOnly,
  rebateRate}` + `feesEnabled` + `feeType: "crypto_fees_v2"`; CLOB
  `/clob-markets/{condition_id}` → `fd {r, e, to}`.
- Several third-party articles quote other percentages; they are ignored.
- **Decision:** fees are read per market at runtime. Unknown fee schedule →
  NO TRADE. The fee model is a pure function tested against the official table.

## 4. WebSocket and real-time data (VERIFIED: SDK source + docs)

| Channel | URL | Protocol |
|---|---|---|
| Market | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | subscribe `{"type":"market","assets_ids":[...],"custom_feature_enabled":bool}`; text `PING` every 10 s → `PONG` (SDK treats 30 s without PONG as stale) |
| User | `wss://ws-subscriptions-clob.polymarket.com/ws/user` | auth object in subscribe message; `order` (PLACEMENT/UPDATE/CANCELLATION) and `trade` events |
| RTDS | `wss://ws-live-data.polymarket.com` | `{"action":"subscribe","subscriptions":[{"topic":...,"type":"update","filters":"{\"symbol\":\"btc/usd\"}"}]}`; `PING` every 5 s |

Market events: `book` (full snapshot + `hash`), `price_change`
(`price_changes[]`, `size == 0` removes the level), `last_trade_price`
(`fee_rate_bps`), `tick_size_change`, and with custom features
`best_bid_ask`, `new_market`, `market_resolved`. Frames may be JSON arrays.

RTDS topics used: `crypto_prices_chainlink` (Chainlink spot),
`crypto_prices_twap_sixty` (Chainlink 60 s TWAP, `full_accuracy_value` is an
E18 integer string), optionally `crypto_prices` (Binance). Payload
`timestamp` is the Chainlink observation time (ms). **RTDS has no snapshot,
history or replay after a disconnect** → gaps must invalidate derived state.

NOT VERIFIED (2026-09-25): the official Python SDK subscribes to these topics
*without* a `filters` field and filters symbols client-side; our runner sends
the per-topic `filters` string shown above. Whether the server honours it the
same way for `crypto_prices_twap_sixty` has not been observed from this
environment. `make diagnose` prints RTDS messages per `type|topic|symbol` so a
paper session shows directly whether TWAP ticks arrive (and under which symbol).

## 5. BTC Up/Down 5m — resolution (VERIFIED on real archived markets)

Discovery: series id `10684`, slug `btc-up-or-down-5m`; event slug
`btc-updown-5m-{start_unix}`; `eventStartTime = start`, `endDate = start + 300 s`.
Markets are created ~24 h in advance.

**Rule history (official changelog):**

| Period (UTC) | Rule id in code | Reference & settlement |
|---|---|---|
| 2026-02-12 → 2026-08-07 | `btc_5m_spot_v1` | Chainlink BTC/USD data stream, single snapshot |
| 2026-08-07 → 2026-08-14 00:00 | `btc_5m_twap30_v2` | Chainlink 30 s TWAP |
| since 2026-08-14 00:00 | `btc_5m_twap60_v3` | Chainlink **60 s TWAP**, for *both* the price to beat and the final price |

Current market text (verbatim, hashed in code):

> This market will resolve to "Up" if the time-weighted average price (TWAP) of
> Bitcoin, generated by Chainlink, of the time range specified in the title is
> greater than or equal to the price at the beginning of that range. Otherwise,
> it will resolve to "Down". …resolution source… BTC/USD TWAP data stream
> available at https://data.chain.link/streams/btc-usd-twap-60s-streams…

plus `cryptoMarketConfig = {id: "btc-5m-twap-60", asset: "btc", duration:
"5m", twapEnabled: true, twapLookbackSeconds: 60}`.

Verified on 11 consecutive resolved markets (fixture
`tests/fixtures/gamma/btc_5m_twap_events_2026-09-23.json`):

- `winner == "Up"  ⇔  finalPrice ≥ priceToBeat` — **11/11**.
- `finalPrice(N) == priceToBeat(N+1)` exactly — **11/11** (consistent with both
  values coming from the same TWAP feed at window boundaries).
- Ties resolve **Up** (`≥`).
- ~~`eventMetadata.priceToBeat` appears only after the window starts~~
  **CORRECTED 2026-09-25 (live observation, see below): it appears only
  after the window has ENDED.** The archived fixture could not show this (all
  its markets were already resolved).
  `finalPrice` can lag the resolution (one market had `outcomePrices`
  `["0","1"]` and no `finalPrice` yet) → the adapter treats resolution as known
  only from `closed` + `umaResolutionStatus == "resolved"` + `outcomePrices`.
- Resolution happened 53–90 s after `endDate` (`closedTime`).

**Live observation 2026-09-25 (public Gamma `/events?slug=…`, read-only,
02:44–02:50 UTC; VERIFIED on 3 consecutive windows):**

| window (UTC) | running window | ~2.5 min after end | ~5 min after end |
|---|---|---|---|
| `btc-updown-5m-1790304000` (02:40–02:45) | no `eventMetadata` (at +275 s) | no `eventMetadata`\* | `{"priceToBeat": 84418.21618781498}` (event `updatedAt` 02:46:53, `closedTime` 02:45:53) |
| `btc-updown-5m-1790304300` (02:45–02:50) | no `eventMetadata` (at +44 s and +153 s) | — | — |
| `btc-updown-5m-1790303700` (02:35–02:40) | — | — | `{"priceToBeat": 84634.68903016155, "finalPrice": 84418.21618781498}` |

\* that response still showed `closed=false` after `closedTime`, i.e. it was
served from a cache; Gamma responses can lag by a minute or more.

Consequences: (1) during a running window Gamma never provides the official
price to beat, so a bot that requires it (`price_to_beat_verified`) can never
trade — this is the root cause of the zero-trade paper session of
2026-09-25 (docs/diagnostics.md); (2) `priceToBeat(N) == finalPrice(N-1)` held
again (84418.216…, 84634.689…), but `finalPrice(N-1)` is published even later,
so it is no in-window substitute either; (3) the only in-window candidate is
the RTDS `crypto_prices_twap_sixty` tick at the window start — **NOT VERIFIED**
to equal `priceToBeat` (the paper runner now records the comparison:
`MarketDataHub.ptb_checks`, shown by `make diagnose`).

The polymarket.com web endpoint
`/api/crypto/crypto-price?symbol=BTC&eventStartTime=…&variant=fiveminute&endDate=…`
(undocumented) returned for 02:35–02:40 `openPrice 84606.43`,
`closePrice 84421.95`, i.e. **not** the settlement values (Gamma: 84634.69 /
84418.22, 3.3 bps apart) — it must not be used as the price to beat.

**Irreducible uncertainty (official docs):** *"Chainlink does not currently
publish the custom feed's sampling boundaries, weighting, rounding, or
missing-input behavior, so do not independently reproduce the value."* → the
fair-value model treats the settlement value as a noisy function of the
observable price path, with an explicit model-error term, and the edge must
survive that error.

**Edge cases observed:** stale "zombie" events (created 2025-12, `closed=false`,
`enableOrderBook=false`, end date long past) are returned by the series query
→ discovery filters on `enableOrderBook`, `acceptingOrders`, end date and
window length.

**Fail-closed rule:** a market is tradable only if its description hash,
resolution source and `cryptoMarketConfig` exactly match a registered rule
version *and* that version is enabled. Any change on Polymarket's side makes the
bot stop trading automatically.

## 6. Geographic / operational constraints (VERIFIED: Help Center, 2026-08-14)

- **39 fully blocked countries including France (FR)**, US, GB, DE, IT, NL,
  BE, AU, JP… plus close-only countries (SG, PL, TH, TW) and blocked regions
  (Ontario, Quebec, BC, Alberta, Crimea…). Note: the page states "39" but its
  list enumerates 40 ISO codes; `ComplianceConfig.blocked_countries` contains
  all 40 (the stricter reading).
- Geoblock endpoint `GET https://polymarket.com/api/geoblock` → JSON with
  `blocked` (bool), `ip`, `country`, `region` (VERIFIED from a real response;
  fixture `tests/fixtures/polymarket/geoblock_blocked_us.json`, IP anonymised).
- CLOB `GET /time` returns integer epoch **seconds** as plain text (VERIFIED);
  clock drift is therefore measured from WebSocket exchange timestamps (ms).
- Using a VPN or similar to bypass restrictions violates Polymarket's ToS
  (§2.1.4).
- Primary servers: `eu-west-2`.
- **Decision:** live trading requires a passing compliance check (operator
  attestation of an eligible jurisdiction **and** Polymarket's geoblock
  endpoint when reachable). This project does not provide, and will not
  provide, any mechanism to circumvent geographic restrictions.

## 7. Anthropic / Claude API (VERIFIED: bundled official `claude-api` skill + PyPI)

- `anthropic` **1.8.0** (2026-09-22), built on `httpx2`, Python ≥ 3.10.
- Default model per official guidance: `claude-opus-5` (configurable; other
  current IDs: `claude-sonnet-5`, `claude-haiku-4-5`, …). Pricing
  (per 1M tokens in/out): Opus 5 $5/$25, Sonnet 5 $2/$10, Haiku 4.5 $1/$5.
- Structured outputs: `output_config={"format": {"type": "json_schema",
  "schema": ...}}` (old `output_format` top-level param deprecated). Response is
  still re-validated locally with Pydantic.
- Must handle `stop_reason == "refusal"` before reading content; optional
  server-side fallbacks (`fallbacks: "default"`, beta
  `server-side-fallback-2026-07-01`). A refusal is treated as REJECT.
- Thinking is on by default on Opus 5; effort is set explicitly (`low` for
  reviews) to bound cost.

## 8. MCP Python SDK (VERIFIED: PyPI + package source)

- `mcp` **2.2.0** (2026-09-07) is the current stable line (spec 2026-07-28).
  `FastMCP` was renamed **`MCPServer`** (`from mcp.server import MCPServer`);
  importing `mcp.server.fastmcp` raises.
- `ToolAnnotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
  `openWorldHint`) are **hints only** — never a security boundary. Security is
  enforced by the tool set we expose and by the Risk Engine behind
  `request_trade`.

## 9. Other library versions (PyPI, 2026-09-23)

pydantic 2.13.5, pydantic-settings 2.15.0, websockets 17.1 (the Polymarket SDK
pins `<16`, so we use 15.x), httpx 0.28.1, numpy 2.5.3 (Python ≥ 3.12),
prometheus-client 0.26.0, pytest 9.1.1, pytest-asyncio 1.4.0, hypothesis
6.168.0, ruff 0.16.8, mypy 2.3.1, bandit 1.9.4, pip-audit 2.10.1,
pre-commit 4.6.2.

## 10. Technical decisions derived from research

| # | Decision | Reason |
|---|---|---|
| D1 | Python 3.12 | Supported by every dependency incl. `polymarket-client` (3.11–3.13) and numpy 2.5. |
| D2 | Polymarket SDK only in the live execution adapter (optional extra) | Blast radius; SDK hazards §1.1; 0.x API churn. |
| D3 | Own WS + REST public-data clients (httpx/websockets) | Explicit gap detection, raw-payload recording for replay, strict fail-closed parsing. |
| D4 | FAK-only execution in live | No verified heartbeat in Python SDK; GTD ≥ 3 min; 5 m markets. |
| D5 | Rule-version registry with exact text hashing | Rules changed twice in August 2026. |
| D6 | TWAP-aware Gaussian baseline + logistic calibration | Settlement is a 60 s TWAP; exact computation unpublished. |
| D7 | No scipy/sklearn/pandas in core | Normal CDF via `math.erf`, logistic regression via numpy Newton/IRLS; fewer dependencies. |
| D8 | stdlib `logging` JSON formatter with redaction (no structlog) | One fewer dependency; redaction is centralized. |
| D9 | Claude called only on high-value events, strict JSON schema, re-validated | Cost control and safety. |
| D10 | Live gated by compliance + promotion gates + multi-flag lock | Geo restrictions; operator control. |

## Sources

- https://docs.polymarket.com/llms.txt, `/v2-migration`, `/trading/fees.md`,
  `/trading/matching-engine.md`, `/trading/manage-orders.md`,
  `/market-data/chainlink-twap.md`, `/changelog/predictions.md`
- https://help.polymarket.com/en/articles/13364163-geographic-restrictions
- https://gamma-api.polymarket.com/events?series_id=10684&closed=true (payload saved as fixture)
- https://clob.polymarket.com/clob-markets/{cid}, `/book?token_id=` (payloads saved as fixtures)
- PyPI: polymarket-client 0.10.0, py-clob-client-v2 1.1.0, py-clob-client 0.34.6, mcp 2.2.0, anthropic 1.8.0
- https://github.com/Polymarket/py-sdk (README, pyproject), https://github.com/Polymarket/agent-skills
