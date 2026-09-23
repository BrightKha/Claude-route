"""Independent watchdog.

Two layers, neither on the strategy's code path:

* :class:`WatchdogEvaluator` — a pure function from a :class:`HealthSnapshot`
  to a list of anomalies (unit-testable, deterministic).
* :class:`Watchdog` — an asyncio task that evaluates periodically and acts:
  halt entries, cancel open orders, trip the kill switch, write incidents.
* :class:`LoopStallDetector` — a daemon *thread* that detects a blocked event
  loop (which the asyncio task could never see) and halts from outside it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from polymarket_bot.audit.jsonable import canonical_dumps
from polymarket_bot.config.app_config import WatchdogConfig
from polymarket_bot.domain.clock import Clock
from polymarket_bot.domain.types import BotState
from polymarket_bot.lifecycle.state_machine import BotStateMachine
from polymarket_bot.watchdog.health import HealthRegistry, HealthSnapshot

log = logging.getLogger(__name__)

HALT_RECOVERABLE = "halt_recoverable"  # auto-resume after resync + reconciliation
HALT_MANUAL = "halt_manual"  # operator must investigate
KILL = "kill"  # trip the kill switch


@dataclass(frozen=True, slots=True)
class Anomaly:
    code: str
    severity: str
    detail: str


class WatchdogEvaluator:
    def __init__(self, config: WatchdogConfig, *, trading_active: Callable[[], bool]) -> None:
        self._cfg = config
        self._trading_active = trading_active

    def evaluate(self, h: HealthSnapshot, *, force: bool = False) -> list[Anomaly]:
        """``force=True`` evaluates data checks even when not trading (recovery gate)."""
        cfg = self._cfg
        out: list[Anomaly] = []
        if h.loop_beat_age_s is not None and h.loop_beat_age_s > cfg.loop_stall_s:
            out.append(Anomaly("event_loop_stall", HALT_MANUAL, f"{h.loop_beat_age_s:.1f}s"))
        if not force and not self._trading_active():
            return out
        if not h.market_stream_connected:
            out.append(Anomaly("market_stream_down", HALT_RECOVERABLE, "disconnected"))
        elif h.market_last_msg_ms is None or (
            h.now_ms - h.market_last_msg_ms > cfg.max_market_data_silence_s * 1000
        ):
            out.append(
                Anomaly("market_stream_silent", HALT_RECOVERABLE, f"last={h.market_last_msg_ms}")
            )
        if not h.reference_stream_connected:
            out.append(Anomaly("reference_stream_down", HALT_RECOVERABLE, "disconnected"))
        elif h.reference_last_msg_ms is None or (
            h.now_ms - h.reference_last_msg_ms > cfg.max_reference_silence_s * 1000
        ):
            out.append(
                Anomaly(
                    "reference_stream_silent", HALT_RECOVERABLE, f"last={h.reference_last_msg_ms}"
                )
            )
        if h.clock_drift_ms is None:
            out.append(Anomaly("clock_drift_unknown", HALT_RECOVERABLE, "no measurement"))
        elif abs(h.clock_drift_ms) > cfg.max_clock_drift_ms:
            out.append(Anomaly("clock_drift", HALT_MANUAL, f"{h.clock_drift_ms}ms"))
        if h.last_reconciliation_ms is None or (
            h.now_ms - h.last_reconciliation_ms > cfg.max_reconciliation_age_s * 1000
        ):
            out.append(
                Anomaly("reconciliation_stale", HALT_RECOVERABLE, f"{h.last_reconciliation_ms}")
            )
        elif not h.last_reconciliation_ok:
            out.append(Anomaly("reconciliation_mismatch", HALT_MANUAL, "last run failed"))
        if h.unknown_orders > 0:
            out.append(Anomaly("unknown_order_state", HALT_MANUAL, f"{h.unknown_orders}"))
        if h.unhandled_exceptions > 0:
            out.append(Anomaly("unhandled_exception", HALT_MANUAL, f"{h.unhandled_exceptions}"))
        return out


CancelAll = Callable[[], Awaitable[bool]]
IncidentWriter = Callable[[str, str, dict[str, object]], None]
KillFn = Callable[[str], None]


class Watchdog:
    def __init__(
        self,
        config: WatchdogConfig,
        registry: HealthRegistry,
        state_machine: BotStateMachine,
        clock: Clock,
        *,
        cancel_all: CancelAll | None,
        write_incident: IncidentWriter,
        engage_kill_switch: KillFn,
    ) -> None:
        self._cfg = config
        self._registry = registry
        self._sm = state_machine
        self._clock = clock
        self._cancel_all = cancel_all
        self._write_incident = write_incident
        self._kill = engage_kill_switch
        self.evaluator = WatchdogEvaluator(
            config, trading_active=lambda: state_machine.state in (BotState.PAPER, BotState.LIVE)
        )
        self.last_anomalies: list[Anomaly] = []
        self._active_codes: frozenset[str] = frozenset()

    @property
    def healthy(self) -> bool:
        return not self.last_anomalies

    async def check_once(self) -> list[Anomaly]:
        """Evaluate and act. Edge-triggered: acts when a *new* anomaly code appears."""
        snap = self._registry.snapshot(self._clock.now_ms())
        anomalies = self.evaluator.evaluate(snap)
        self.last_anomalies = anomalies
        codes = frozenset(a.code for a in anomalies)
        new_codes = codes - self._active_codes
        self._active_codes = codes
        if new_codes:
            await self._act([a for a in anomalies if a.code in new_codes], snap)
        return anomalies

    def recovery_blockers(self) -> list[Anomaly]:
        """Anomalies that forbid resuming from HALTED (evaluated as if trading)."""
        return self.evaluator.evaluate(self._registry.snapshot(self._clock.now_ms()), force=True)

    async def _act(self, anomalies: list[Anomaly], snap: HealthSnapshot) -> None:
        severities = {a.severity for a in anomalies}
        summary = "; ".join(f"{a.code}({a.detail})" for a in anomalies)
        body: dict[str, object] = {
            "anomalies": [a.code for a in anomalies],
            "detail": summary,
            "health": canonical_dumps(snap),
        }
        if KILL in severities:
            self._write_incident("critical", "watchdog_kill", body)
            self._kill(f"watchdog: {summary}")
        elif self._sm.state in (BotState.PAPER, BotState.LIVE, BotState.SYNCING):
            manual = HALT_MANUAL in severities
            self._write_incident("critical" if manual else "warning", "watchdog_halt", body)
            self._sm.halt(f"watchdog: {summary}", manual_only=manual)
        if self._cfg.cancel_orders_on_halt and self._cancel_all is not None:
            try:
                ok = await self._cancel_all()
            except Exception:
                log.exception("watchdog cancel_all failed")
                ok = False
            if not ok:
                self._write_incident("critical", "watchdog_cancel_failed", body)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.check_once()
            except Exception:
                log.exception("watchdog iteration failed")
                self._registry.exception()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._cfg.check_interval_s)


class LoopStallDetector:
    """Detects a blocked asyncio loop from a separate OS thread."""

    def __init__(
        self,
        registry: HealthRegistry,
        state_machine: BotStateMachine,
        stall_s: float,
        incident_dir: Path,
    ) -> None:
        self._registry = registry
        self._sm = state_machine
        self._stall_s = stall_s
        self._incident_dir = incident_dir
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="loop-stall-detector", daemon=True)
        self.triggered = False

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(min(1.0, self._stall_s / 2)):
            snap = self._registry.snapshot(int(time.time() * 1000))
            age = snap.loop_beat_age_s
            if age is not None and age > self._stall_s and not self.triggered:
                self.triggered = True
                self._incident_dir.mkdir(parents=True, exist_ok=True)
                path = self._incident_dir / f"loop_stall_{int(time.time())}.json"
                path.write_text(canonical_dumps({"loop_beat_age_s": age}), encoding="utf-8")
                try:
                    self._sm.halt(f"event loop stalled {age:.1f}s", manual_only=True)
                except Exception:  # never let the detector thread die silently
                    log.exception("loop stall halt failed")
