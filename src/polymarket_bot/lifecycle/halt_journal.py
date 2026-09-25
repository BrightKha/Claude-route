"""Explicit, hash-chained record of every halt and of every recovery from it.

A state-change listener. For each transition into HALTED / KILL_SWITCH it
appends a ``halt`` audit event (timestamp, cause, source component, exact
condition, category); leaving HALTED appends ``recovery_started`` and, once the
bot trades again, ``recovered`` — both carrying the halt id and its original
cause, so an automatic recovery can never hide why the bot stopped
(docs/incident-response.md).
"""

from __future__ import annotations

from collections import deque
from typing import Any

from polymarket_bot.audit.audit_log import AuditLog
from polymarket_bot.domain.types import BotState
from polymarket_bot.lifecycle.state_machine import StateChange

HISTORY_KEPT = 50
HALT_STATES = (BotState.HALTED, BotState.KILL_SWITCH)
TRADING_STATES = (BotState.PAPER, BotState.LIVE)
_CATEGORY_BY_ANOMALY = {
    "market_stream_down": "feed",
    "market_stream_silent": "feed",
    "reference_stream_down": "feed",
    "reference_stream_silent": "feed",
    "clock_drift": "feed",
    "clock_drift_unknown": "feed",
    "reconciliation_stale": "reconciliation",
    "reconciliation_mismatch": "reconciliation",
    "unknown_order_state": "execution",
    "unhandled_exception": "software",
    "event_loop_stall": "runtime",
}


def halt_category(change: StateChange) -> str:
    """watchdog / risk / feed / execution / reconciliation / ... for one halt."""
    details = change.details or {}
    anomalies = details.get("anomalies")
    if isinstance(anomalies, list) and anomalies:
        cats = {
            _CATEGORY_BY_ANOMALY.get(str(a.get("code")), "watchdog")
            for a in anomalies
            if isinstance(a, dict)
        }
        return "+".join(sorted(cats)) or "watchdog"
    if change.to_state is BotState.KILL_SWITCH:
        if "loss limit" in change.reason:
            return "risk"
        if "invariant" in change.reason:
            return "execution"
        return "kill_switch"
    return {
        "execution": "execution",
        "reconciliation": "reconciliation",
        "resolution": "data",
        "watchdog.loop_stall": "runtime",
        "lifecycle": "lifecycle",
    }.get(change.component, change.component)


class HaltJournal:
    def __init__(self, audit: AuditLog) -> None:
        self._audit = audit
        self._seq = 0
        self._open: dict[str, Any] | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_KEPT)

    def __call__(self, change: StateChange) -> None:
        if change.to_state in HALT_STATES:
            self._on_halt(change)
        elif change.from_state is BotState.HALTED and self._open is not None:
            self._open["recovery_started_ms"] = change.ts_ms
            self._audit.append(
                "recovery_started",
                {
                    "halt_id": self._open["halt_id"],
                    "halt_cause": self._open["cause"],
                    "halt_component": self._open["component"],
                    "halted_at_ms": self._open["at_ms"],
                    "halted_for_ms": change.ts_ms - self._open["at_ms"],
                    "to_state": change.to_state.value,
                    "reason": change.reason,
                    "component": change.component,
                    "condition": change.details,
                },
            )
        elif change.to_state in TRADING_STATES and self._open is not None:
            rec = self._open
            rec["recovered_ms"] = change.ts_ms
            rec["downtime_ms"] = change.ts_ms - rec["at_ms"]
            self._audit.append(
                "recovered",
                {
                    "halt_id": rec["halt_id"],
                    "halt_cause": rec["cause"],
                    "halt_component": rec["component"],
                    "halted_at_ms": rec["at_ms"],
                    "recovered_ms": change.ts_ms,
                    "downtime_ms": rec["downtime_ms"],
                    "to_state": change.to_state.value,
                },
            )
            self._open = None

    def _on_halt(self, change: StateChange) -> None:
        self._seq += 1
        rec: dict[str, Any] = {
            "halt_id": f"halt-{change.ts_ms}-{self._seq}",
            "at_ms": change.ts_ms,
            "from_state": change.from_state.value,
            "to_state": change.to_state.value,
            "cause": change.reason,
            "component": change.component,
            "category": halt_category(change),
            "condition": change.details,
            "manual_only": change.manual_only,
        }
        if self._open is not None:
            rec["supersedes"] = self._open["halt_id"]  # halted again before trading resumed
        self._open = rec
        self.history.append(rec)
        self._audit.append("halt", rec)
