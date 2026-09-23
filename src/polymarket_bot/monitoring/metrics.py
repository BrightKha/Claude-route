"""Prometheus metrics (optional; ``monitoring.metrics_enabled``).

Read-only view of the running core, refreshed by the runner. Bound to
127.0.0.1 by default; expose it further only behind authentication. No secret,
wallet address or order id is ever used as a label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Gauge, start_http_server

from polymarket_bot.domain.types import BotState

if TYPE_CHECKING:
    from polymarket_bot.app.build import Assembly


class BotMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.state = Gauge("bot_state", "1 for the current lifecycle state", ["state"], registry=r)
        self.kill_switch = Gauge("bot_kill_switch_engaged", "kill switch engaged", registry=r)
        self.equity = Gauge("bot_equity_usd", "equity marked at best bid", registry=r)
        self.risk_equity = Gauge("bot_risk_equity_usd", "equity used by loss limits", registry=r)
        self.cash = Gauge("bot_cash_usd", "cash", registry=r)
        self.exposure = Gauge("bot_exposure_usd", "open cost basis", registry=r)
        self.positions = Gauge("bot_open_positions", "open positions", registry=r)
        self.open_orders = Gauge("bot_open_orders", "open or unknown orders", registry=r)
        self.unknown_orders = Gauge("bot_unknown_orders", "orders in UNKNOWN state", registry=r)
        self.anomalies = Gauge("bot_watchdog_anomalies", "active watchdog anomalies", registry=r)
        self.steps = Counter("bot_decision_steps", "decision steps", registry=r)
        self.entries = Counter("bot_entries_submitted", "entry orders submitted", registry=r)
        self.exits = Counter("bot_exits_submitted", "exit orders submitted", registry=r)
        self.rejections = Counter("bot_risk_rejections", "risk engine rejections", registry=r)
        self._last: dict[str, int] = {}

    def serve(self, bind: str, port: int) -> None:
        start_http_server(port, addr=bind, registry=self.registry)

    def _inc(self, counter: Counter, key: str, total: int) -> None:
        delta = total - self._last.get(key, 0)
        if delta > 0:
            counter.inc(delta)
        self._last[key] = total

    def update(self, asm: Assembly) -> None:
        core = asm.core
        for s in BotState:
            self.state.labels(state=s.value).set(1 if asm.state.state is s else 0)
        self.kill_switch.set(1 if asm.kill_switch.is_engaged() else 0)
        pf = core.portfolio
        self.equity.set(float(pf.equity_usd))
        self.risk_equity.set(float(pf.risk_equity_usd))
        self.cash.set(float(pf.cash_usd))
        self.exposure.set(float(pf.exposure_usd))
        self.positions.set(len(pf.positions))
        self.open_orders.set(len(core.execution.open_orders()))
        self.unknown_orders.set(core.execution.unknown_count())
        self.anomalies.set(len(asm.watchdog.last_anomalies))
        self._inc(self.steps, "steps", core.stats.steps)
        self._inc(self.entries, "entries", core.stats.entries_submitted)
        self._inc(self.exits, "exits", core.stats.exits_submitted)
        self._inc(self.rejections, "rejections", core.stats.risk_rejections)
