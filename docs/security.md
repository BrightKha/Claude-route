# Security

## Assets and threats

| Asset | Threats | Controls |
|---|---|---|
| Wallet private key / CLOB API credentials | leakage via logs, prompts, exceptions, git, MCP, reports | read only in `security/secrets.py`; used only by `adapters/polymarket_live.py` (static test); `Secret` wrapper never renders; value-based redaction on every log record, exception hook, recorder bot events, MCP outputs, Claude prompts; secret scanner (`scripts/check_secrets.py`) on tracked/staged files; `.env` git-ignored; YAML loader rejects secret-like keys |
| Anthropic API key | leakage, cost abuse | read only by `llm/claude_client.py`; key only in the HTTP header (tested); daily/hourly/minute budgets with worst-case pre-reservation |
| Funds | unauthorized orders, runaway loops, bad fills | Risk Engine on every order (entries and exits); hard caps in code; FAK-only; write-ahead + idempotent execution, no blind retries; kill switch; loss limits; reconciliation; live lock |
| Integrity of decisions | tampering, silent state drift | hash-chained audit log; reconciliation; invariant checks ⇒ kill switch |
| Operator machine | supply chain | pinned deps (`uv.lock`), exact SDK pin, `pip-audit`, `bandit`, minimal Docker image, non-root user |

## Claude and MCP boundaries

* Claude sees only a **whitelisted numeric context** (`llm/schemas.py:ReviewContext`):
  no wallet, balance, key, order id or configuration value. Before sending, the
  prompt is checked against the redaction registry; a match is never sent.
* Claude's output is a strict schema. Refusals, truncation and schema errors
  never approve. An approval can only **tighten** the candidate (lower
  probability band); size and limits are untouched; the Risk Engine re-checks.
* Market text (question) is external data; the system prompt says so, and a
  prompt injection could at worst make Claude approve a candidate that the
  deterministic pipeline had already approved, or reject one.
* The MCP server: closed tool list (tested), read-only DB, proposals only,
  rate-limited, validated (slug pattern, outcome, notional ≤ hard cap),
  refuses to start with secrets in its environment, stdio only.
  **There is no tool to place raw orders, cancel, withdraw, transfer, change
  risk configuration, reset the kill switch or read secrets.** MCP annotations
  are hints; the enforcement is that these code paths do not exist.

## Polymarket SDK hazards (docs/research.md §1.1)

1. `place_*_order` may send an on-chain `approve(max)` and re-post ⇒ the adapter
   only signs (`create_market_order`) and posts (`post_order`).
2. `AsyncSecureClient.create()` may deploy a Deposit Wallet, **even with an
   explicit wallet address** equal to the signer's default wallet ⇒ the adapter
   uses `_create()` (no wallet readiness step); a test pins the method.
3. The secure client exposes transfer/withdraw/redeem/execute ⇒ the SDK object
   is private to the adapter; a fake-client test asserts only whitelisted
   methods are called.
4. SDK streams reconnect silently ⇒ our own WebSocket client surfaces every gap.

## Secrets handling rules

* Provide secrets only through the process environment of the bot (never in
  YAML, never in `.env` committed files, never to the MCP process).
* `POLYMARKET_WALLET_ADDRESS` must be an existing, **dedicated** wallet that
  holds nothing but the bot's working capital; the live bootstrap refuses a
  wallet with positions or open orders.
* Rotate keys immediately if any redaction marker (`[REDACTED]`) appears where
  a secret could have been, or if the secret scanner ever fires
  (see incident-response.md).

## Compliance

Live trading requires an operator attestation of an eligible jurisdiction and,
when reachable, Polymarket's geoblock endpoint reporting `blocked: false`.
Using a VPN or similar to bypass restrictions violates Polymarket's terms; this
project provides no such mechanism and never will.
