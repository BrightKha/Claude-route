# Incident response

First rule: **when in doubt, engage the kill switch** — `make kill-switch`
(or `touch <data_dir>/KILL_SWITCH`). It blocks all entries immediately, is
persisted, and only a documented manual reset lifts it.

Useful commands:

```bash
python -m polymarket_bot.app status                  # state, kill switch, portfolio, health, incidents, audit check
python -m polymarket_bot.app kill-switch status
python -m polymarket_bot.app kill-switch reset --operator NAME \
    --note "what happened, what was checked" --confirm I-HAVE-INVESTIGATED-AND-ACCEPT-RESET
```

## Runbooks

| Symptom / incident | Automatic reaction | Operator action |
|---|---|---|
| **Kill switch engaged** (loss limit, invariant violation) | entries blocked; exits only at risk-exit prices | read `status` incidents and `audit.jsonl`; verify positions on the venue; understand the cause; reset only with a written note; restart |
| **Order state UNKNOWN** (timeout / ambiguous ack) | no new orders; venue queried; HALTED (manual) after `unknown_state_timeout_s` | stop the bot; `orders list`; check the order on Polymarket (open orders, trades). If it never reached the book: `orders resolve-no-fill --intent ID --operator NAME --note "..."`. If it filled: flatten the position on the venue by hand, then resolve it the same way (reconciliation re-checks at restart). Never resubmit blindly |
| **Reconciliation mismatch** | entries blocked at once; HALTED (manual) if confirmed on re-check | compare venue positions/collateral with `status`; an **external order** on the wallet ⇒ assume key compromise (below) |
| **Market/reference stream down or silent** | books invalidated, HALTED (auto-recovery after resync, rate-limited) | if recoveries exceed the hourly limit the halt becomes manual: check connectivity / Polymarket status |
| **Clock drift beyond limit** | HALTED (manual) | fix NTP; restart |
| **Event loop stall** | HALTED (manual) by the stall-detector thread; incident file written | inspect CPU/IO, logs; restart |
| **Resolution anomaly** (official outcome inconsistent with the rule) | HALTED (manual) | re-read the market rules on Polymarket; update `strategies/btc_5m/resolution.py` and `docs/research.md` only with evidence |
| **Unrecognised market rule text** | market not tracked (rejection counted) | check whether Polymarket changed the rule; register a new rule version only after verification on archived markets |
| **Claude unavailable / budget exhausted** | advisory: trade only if allowed without review; required: no trades | none required; check API status and budget |
| **Geoblock / compliance failure** | live refuses to start | do not work around it |
| **Suspected secret leak** (scanner hit, key in a log, external order) | — | engage kill switch; revoke CLOB API keys; move remaining funds from the wallet with a separate, trusted tool; rotate `ANTHROPIC_API_KEY`; purge the leaked artifact (git history rewrite if needed); review audit log |

## After any incident

1. Keep `state.sqlite`, `audit.jsonl` (verify with `status` → `audit_log.valid`),
   the recording of the session and logs.
2. Replay the recorded session to reproduce the decision path.
3. Write the timeline and cause in the kill-switch reset note / an incident
   record; add a regression test before resuming.
