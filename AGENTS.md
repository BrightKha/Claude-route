# AGENTS.md — roles and boundaries

| Agent / component | May | May NOT |
|---|---|---|
| **Claude (runtime reviewer)** `llm/` | Approve / reject / NO_OP a *pre-validated* candidate; tighten probability, shorten holding time, reduce size multiplier (≤ 1); add risks and invalidators to the audit record. | Increase size, exposure, limits or price; see secrets; execute; change config; be required for exits. |
| **Claude (via MCP)** `mcp_server/` | Read status, positions, orders, PnL, risk state, health, events; submit a *proposal* with `request_trade` / `request_close`. | Execute raw orders, withdraw/transfer, change risk config, touch kill switch, change mode, read secrets, run shell commands. The MCP process refuses to start if trading secrets are in its environment and opens the DB read-only except the proposal inbox. |
| **Claude Code (developer agent)** | Edit code under review, run tests, write research, propose features/models/thresholds. | Promote a strategy or policy to production, enable live, commit secrets, weaken tests or safety checks. |
| **Risk Engine** `risk/` | Final say on every order (entries *and* exits). Deterministic, pure, versioned, hashed policy. | Call an LLM or the network. |
| **Execution Engine** `execution/` | Submit only `RiskDecision`-approved orders, idempotently; reconcile unknown states. | Retry blindly, exceed `max_size` / `max_price`, submit without a recorded intent. |
| **Exit Engine** `exits/` | Close positions from explicit, configurable rules without waiting for Claude. | Open or increase positions. |
| **Watchdog** `watchdog/` | Halt entries, cancel orders, trip kill switch, write incidents. | Resume trading after a kill switch (manual only). |
| **Operator (human)** | Fund the dedicated wallet, set live flags, approve promotions, reset the kill switch. | — |

Escalation path: anomaly → watchdog → HALTED (entries blocked) →
reconciliation → auto-resume only for recoverable data outages; everything
else waits for the operator.
