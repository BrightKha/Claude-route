"""Thread-safe health registry written by components, read by the watchdog.

Components only *report*; the watchdog decides. The registry uses the
injected clock for data timestamps and ``time.monotonic`` for the event-loop
heartbeat (a loop stall must be detected in real time even in replay).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    now_ms: int
    loop_beat_age_s: float | None
    market_stream_connected: bool
    market_last_msg_ms: int | None
    reference_stream_connected: bool
    reference_last_msg_ms: int | None
    last_reconciliation_ms: int | None
    last_reconciliation_ok: bool
    clock_drift_ms: int | None
    unknown_orders: int
    unhandled_exceptions: int
    last_decision_ms: int | None
    last_pnl_update_ms: int | None
    extra: dict[str, str] = field(default_factory=dict)


class HealthRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop_beat: float | None = None
        self._market_connected = False
        self._market_last: int | None = None
        self._ref_connected = False
        self._ref_last: int | None = None
        self._recon_ms: int | None = None
        self._recon_ok = False
        self._drift: int | None = None
        self._unknown_orders = 0
        self._exceptions = 0
        self._last_decision: int | None = None
        self._last_pnl: int | None = None
        self._extra: dict[str, str] = {}

    def beat_loop(self) -> None:
        with self._lock:
            self._loop_beat = time.monotonic()

    def market_stream(self, *, connected: bool, msg_ms: int | None = None) -> None:
        with self._lock:
            self._market_connected = connected
            if msg_ms is not None:
                self._market_last = msg_ms

    def reference_stream(self, *, connected: bool, msg_ms: int | None = None) -> None:
        with self._lock:
            self._ref_connected = connected
            if msg_ms is not None:
                self._ref_last = msg_ms

    def reconciliation(self, *, ts_ms: int, ok: bool) -> None:
        with self._lock:
            self._recon_ms = ts_ms
            self._recon_ok = ok

    def clock_drift(self, drift_ms: int | None) -> None:
        with self._lock:
            self._drift = drift_ms

    def unknown_orders(self, count: int) -> None:
        with self._lock:
            self._unknown_orders = count

    def exception(self) -> None:
        with self._lock:
            self._exceptions += 1

    def decision(self, ts_ms: int) -> None:
        with self._lock:
            self._last_decision = ts_ms

    def pnl_update(self, ts_ms: int) -> None:
        with self._lock:
            self._last_pnl = ts_ms

    def note(self, key: str, value: str) -> None:
        with self._lock:
            self._extra[key] = value

    def snapshot(self, now_ms: int) -> HealthSnapshot:
        with self._lock:
            beat_age = None if self._loop_beat is None else time.monotonic() - self._loop_beat
            return HealthSnapshot(
                now_ms=now_ms,
                loop_beat_age_s=beat_age,
                market_stream_connected=self._market_connected,
                market_last_msg_ms=self._market_last,
                reference_stream_connected=self._ref_connected,
                reference_last_msg_ms=self._ref_last,
                last_reconciliation_ms=self._recon_ms,
                last_reconciliation_ok=self._recon_ok,
                clock_drift_ms=self._drift,
                unknown_orders=self._unknown_orders,
                unhandled_exceptions=self._exceptions,
                last_decision_ms=self._last_decision,
                last_pnl_update_ms=self._last_pnl,
                extra=dict(self._extra),
            )
