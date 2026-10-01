"""Halt / recovery history rebuilt from the audit log (read-only; old logs included).

Every transition is in the hash-chained audit log as ``state_change`` (with its
reason). Since the halt journal (``lifecycle/halt_journal.py``) each halt also
has an explicit ``halt`` event with the source component, the exact condition
and a category; older logs only have the reason, which for watchdog halts
already lists the anomalies. Watchdog incidents add the health snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from polymarket_bot.monitoring.pipeline import iso_ms

HALT_STATES = ("HALTED", "KILL_SWITCH")
TRADING_STATES = ("PAPER", "LIVE")


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def halt_history(
    audit_path: Path, incidents: list[dict[str, Any]] | None = None, *, last: int = 20
) -> list[dict[str, Any]]:
    """One row per halt: when, cause, component, condition, category, recovery.

    Built from ``state_change`` records and/or the journal's ``halt`` /
    ``recovery_started`` / ``recovered`` events (either source is enough).
    """
    by_ts = {
        int(i["ts_ms"]): i["body"]
        for i in incidents or []
        if i.get("kind") in ("watchdog_halt", "watchdog_kill")
    }
    rows: list[dict[str, Any]] = []
    open_row: dict[str, Any] | None = None

    def start(ts: int, frm: Any, to: Any, cause: Any, manual: Any) -> dict[str, Any]:
        row = {
            "halt_at": iso_ms(ts),
            "halt_ms": ts,
            "from_state": frm,
            "to_state": to,
            "cause": cause,
            "component": "unknown (log predates halt journal)",
            "condition": None,
            "manual_only": manual,
            "recovery_started_at": None,
            "recovered_at": None,
            "downtime_s": None,
        }
        rows.append(row)
        return row

    def same(row: dict[str, Any] | None, ts: int, cause: Any) -> bool:
        return row is not None and row["halt_ms"] == ts and row["cause"] == cause

    for rec in _records(audit_path):
        kind, payload = rec.get("kind"), rec.get("payload") or {}
        if kind == "halt":
            ts, cause = int(payload.get("at_ms", 0)), payload.get("cause")
            if not same(open_row, ts, cause):
                open_row = start(
                    ts,
                    payload.get("from_state"),
                    payload.get("to_state"),
                    cause,
                    payload.get("manual_only"),
                )
            assert open_row is not None
            open_row.update(
                halt_id=payload.get("halt_id"),
                component=payload.get("component", open_row["component"]),
                category=payload.get("category"),
                condition=payload.get("condition") or open_row["condition"],
            )
        elif kind in ("recovery_started", "recovered") and open_row is not None:
            ts = int(payload.get("recovered_ms") or rec.get("ts_ms") or 0)
            if kind == "recovery_started" and open_row["recovery_started_at"] is None:
                open_row["recovery_started_at"] = iso_ms(int(rec.get("ts_ms") or 0))
                open_row["recovery_reason"] = payload.get("reason")
            if kind == "recovered":
                open_row["recovered_at"] = iso_ms(ts)
                open_row["downtime_s"] = round((ts - open_row["halt_ms"]) / 1000, 1)
                open_row = None
        elif kind == "state_change":
            change = payload.get("change") or {}
            to_state, from_state = change.get("to_state"), change.get("from_state")
            ts = int(change.get("ts_ms") or rec.get("ts_ms") or 0)
            if to_state in HALT_STATES:
                open_row = start(
                    ts, from_state, to_state, change.get("reason"), change.get("manual_only")
                )
                if change.get("component"):
                    open_row["component"] = change["component"]
                open_row["condition"] = change.get("details")
            elif from_state == "HALTED" and open_row is not None:
                if open_row["recovery_started_at"] is None:
                    open_row["recovery_started_at"] = iso_ms(ts)
                    open_row["recovery_reason"] = change.get("reason")
            elif to_state in TRADING_STATES and open_row is not None:
                open_row["recovered_at"] = iso_ms(ts)
                open_row["downtime_s"] = round((ts - open_row["halt_ms"]) / 1000, 1)
                open_row = None
    for row in rows:
        incident = by_ts.get(row["halt_ms"])
        if incident is not None and not row.get("condition"):
            row["condition"] = {
                "anomalies": incident.get("anomalies"),
                "detail": incident.get("detail"),
            }
        if not row.get("category"):
            row["category"] = _category_from_cause(str(row["cause"]))
    return rows[-last:]


def _category_from_cause(cause: str) -> str:
    """Best effort for logs written before the halt journal existed."""
    if cause.startswith("watchdog:"):
        feed = ("market_stream", "reference_stream", "clock_drift")
        return "feed" if any(k in cause for k in feed) else "watchdog"
    if "loss limit" in cause:
        return "risk"
    if "reconciliation" in cause:
        return "reconciliation"
    if "order state unknown" in cause or "invariant" in cause:
        return "execution"
    return "other"
