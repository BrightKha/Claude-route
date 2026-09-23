"""Risk decision -> execution engine -> paper exchange -> portfolio -> reconciliation."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from polymarket_bot.adapters.paper_exchange import PaperExchange
from polymarket_bot.audit.audit_log import AuditLog
from polymarket_bot.config.app_config import (
    ExecutionConfig,
    PaperExchangeConfig,
    ReconciliationConfig,
)
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.decisions import RiskDecision
from polymarket_bot.domain.orders import Fill, OrderRecord
from polymarket_bot.domain.types import OrderPurpose, OrderStatus, Side
from polymarket_bot.execution.engine import ExecutionEngine
from polymarket_bot.portfolio.portfolio import Portfolio
from polymarket_bot.reconciliation.reconciler import LocalState, Reconciler
from polymarket_bot.storage.sqlite_store import StateStore
from tests.factories import COND, DOWN, T0, UP, make_book, make_market

D = Decimal
NOW = T0 + 60_000


class Chain:
    def __init__(self, tmp: Path) -> None:
        self.clock = SimulatedClock(NOW)
        self.market = make_market()
        self.book = make_book(
            UP, [("0.60", "50"), ("0.59", "100")], [("0.62", "10"), ("0.63", "20")], NOW
        )
        cfg = PaperExchangeConfig.model_validate(
            {"initial_balance_usd": "100", "latency_jitter_ms": 0}
        )
        self.exchange = PaperExchange(cfg, self.clock, lambda t: self.book if t == UP else None)
        self.portfolio = Portfolio.with_cash(D("100"), NOW)
        self.engine = ExecutionEngine(
            self.exchange,
            StateStore(tmp / "state.sqlite"),
            AuditLog(tmp / "audit.jsonl", self.clock, fsync=False),
            self.clock,
            ExecutionConfig(),
            on_fill=self._on_fill,
        )
        self.reconciler = Reconciler(ReconciliationConfig())

    def _on_fill(self, fill: Fill, rec: OrderRecord) -> None:
        self.portfolio.apply_fill(fill, market_slug=self.market.slug, window_end_ms=0)

    async def run_until_idle(self) -> None:
        for _ in range(10):
            self.clock.advance_to(self.clock.now_ms() + 100)
            self.engine.apply_events(await self.exchange.poll_events())

    async def reconcile(self) -> bool:
        local = LocalState(
            positions={t: p.shares for t, p in self.portfolio.positions.items()},
            cash_usd=self.portfolio.cash_usd,
            open_order_ids=frozenset(),
            known_order_ids=self.engine.known_exchange_ids(),
        )
        report = self.reconciler.compare(
            local, await self.exchange.account_snapshot(), self.clock.now_ms()
        )
        return report.ok


def _decision(**kw: object) -> RiskDecision:
    base = RiskDecision(
        decision_id="rd-1",
        allowed=True,
        reasons=(),
        checks=(),
        purpose=OrderPurpose.ENTRY,
        side=Side.BUY,
        token_id=UP,
        condition_id=COND,
        max_size_usd=D("10"),
        max_shares=D("16"),
        limit_price=D("0.63"),
        risk_version="risk-1.0.0",
        policy_hash="p",
        timestamp_ms=NOW,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


async def test_entry_exit_and_settlement_stay_reconciled(tmp_path: Path) -> None:
    c = Chain(tmp_path)
    rec = await c.engine.execute(_decision(), c.market, "Up")
    assert rec.status is OrderStatus.LIVE
    await c.run_until_idle()
    final = c.engine.orders[rec.intent.intent_id]
    assert final.status is OrderStatus.FILLED
    assert final.filled_shares == c.portfolio.positions[UP].shares
    assert c.engine.violations == []
    assert await c.reconcile()

    held = c.portfolio.positions[UP].shares
    half = (held / 2).quantize(D("0.01"))
    exit_decision = _decision(
        decision_id="rd-2",
        purpose=OrderPurpose.EXIT,
        side=Side.SELL,
        max_size_usd=D("0"),
        max_shares=half,
        limit_price=D("0.55"),
        timestamp_ms=c.clock.now_ms(),
    )
    await c.engine.execute(exit_decision, c.market, "Up")
    await c.run_until_idle()
    assert c.portfolio.positions[UP].shares == held - half
    assert await c.reconcile()

    c.portfolio.settle(COND, "Up", c.clock.now_ms())
    c.exchange.settle(UP, DOWN)
    assert await c.reconcile()
    assert c.portfolio.positions == {}
    assert c.engine.violations == []


async def test_divergence_between_ledgers_is_detected(tmp_path: Path) -> None:
    c = Chain(tmp_path)
    await c.engine.execute(_decision(), c.market, "Up")
    await c.run_until_idle()
    assert await c.reconcile()
    c.exchange.positions[UP] -= D("1")  # e.g. a missed fill or manual interference
    assert not await c.reconcile()
