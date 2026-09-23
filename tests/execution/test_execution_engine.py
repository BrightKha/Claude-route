"""Execution Engine safety: write-ahead, idempotency, UNKNOWN handling, invariants."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from polymarket_bot.audit.audit_log import AuditLog, verify_audit_log
from polymarket_bot.config.app_config import ExecutionConfig
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.decisions import RiskDecision
from polymarket_bot.domain.market import MarketDefinition
from polymarket_bot.domain.orders import (
    ExecutionEvent,
    Fill,
    OrderAck,
    OrderIntent,
    OrderRecord,
    OrderUpdate,
)
from polymarket_bot.domain.types import OrderPurpose, OrderStatus, Side
from polymarket_bot.execution.engine import ExecutionEngine, ExecutionError
from polymarket_bot.ports import CancelResult
from polymarket_bot.storage.sqlite_store import StateStore
from tests.factories import COND, DOWN, T0, UP, make_market

D = Decimal
NOW = T0 + 60_000


class FakeVenue:
    """Scriptable TradingProvider. Counts submissions so tests can prove no resubmission."""

    name = "fake"

    def __init__(self) -> None:
        self.mode = "ack"  # ack | reject | ambiguous | hang | raise
        self.submitted: list[OrderIntent] = []
        self.history: list[ExecutionEvent] = []
        self.find_raises = False

    async def submit(self, intent: OrderIntent, market: MarketDefinition) -> OrderAck:
        self.submitted.append(intent)
        if self.mode == "hang":
            await asyncio.sleep(3600)
        if self.mode == "raise":
            raise ConnectionError("reset by peer")
        if self.mode == "reject":
            return OrderAck(
                intent.intent_id, False, OrderStatus.REJECTED, None, D(0), None, D(0), NOW,
                error_code="400", error_message="invalid",
            )  # fmt: skip
        return OrderAck(
            intent.intent_id,
            True,
            OrderStatus.LIVE,
            f"x-{intent.intent_id}",
            D(0),
            None,
            D(0),
            NOW,
            ambiguous=self.mode == "ambiguous",
        )

    async def cancel(self, exchange_order_id: str) -> CancelResult:
        return CancelResult((exchange_order_id,), {}, True)

    async def cancel_all(self) -> CancelResult:
        return CancelResult((), {}, True)

    async def poll_events(self) -> list[ExecutionEvent]:
        return []

    async def find_orders_since(self, token_id: str, since_ms: int) -> list[ExecutionEvent]:
        if self.find_raises:
            raise OSError("venue unreachable")
        return [e for e in self.history if e.ts_ms >= since_ms]


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
        limit_price=D("0.629"),
        risk_version="risk-1.0.0",
        policy_hash="p",
        timestamp_ms=NOW,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def _exit_decision(**kw: object) -> RiskDecision:
    return _decision(
        decision_id="rd-exit",
        purpose=OrderPurpose.EXIT,
        side=Side.SELL,
        max_size_usd=D("0"),
        max_shares=D("16"),
        limit_price=D("0.551"),
        **kw,
    )


class Env:
    def __init__(self, tmp: Path, **cfg: object) -> None:
        self.clock = SimulatedClock(NOW)
        self.venue = FakeVenue()
        self.store = StateStore(tmp / "state.sqlite")
        self.audit = AuditLog(tmp / "audit.jsonl", self.clock, fsync=False)
        self.fills: list[tuple[Fill, OrderRecord]] = []
        self.cfg = ExecutionConfig.model_validate({"submit_timeout_s": 0.05, **cfg})
        self.engine = self.new_engine()

    def new_engine(self) -> ExecutionEngine:
        return ExecutionEngine(
            self.venue,
            self.store,
            self.audit,
            self.clock,
            self.cfg,
            on_fill=lambda f, r: self.fills.append((f, r)),
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _fill(
    rec: OrderRecord, n: int = 0, price: str = "0.62", shares: str = "5", **kw: object
) -> Fill:
    base = Fill(
        fill_id=f"f-{n}",
        intent_id=rec.intent.intent_id,
        exchange_order_id=rec.exchange_order_id,
        condition_id=COND,
        token_id=rec.intent.token_id,
        outcome=rec.intent.outcome,
        side=rec.intent.side,
        price=D(price),
        shares=D(shares),
        fee_usd=D("0.08"),
        ts_ms=NOW + 300,
        liquidity="taker",
        source="test",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def _update(rec: OrderRecord, status: OrderStatus, cum: str) -> OrderUpdate:
    assert rec.exchange_order_id is not None
    return OrderUpdate(rec.exchange_order_id, rec.intent.intent_id, status, D(cum), NOW + 400)


# ---------------------------------------------------------------- submission
async def test_write_ahead_then_single_submission(env: Env) -> None:
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    assert rec.status is OrderStatus.LIVE
    assert len(env.venue.submitted) == 1
    (stored,) = env.store.load_orders()
    assert stored.intent.decision_id == "rd-1"
    assert stored.status is OrderStatus.LIVE
    kinds = env.audit.path.read_text().splitlines()
    assert '"order_submit"' in kinds[0] and '"order_ack"' in kinds[1]


async def test_limit_price_is_rounded_towards_safety(env: Env) -> None:
    buy = env.engine.build_intent(_decision(), make_market(), "Up")
    assert buy.limit_price == D("0.62")  # 0.629 floored: never pay more than approved
    sell = env.engine.build_intent(_exit_decision(), make_market(), "Up")
    assert sell.limit_price == D("0.56")  # 0.551 ceiled: never sell for less than approved
    assert sell.sell_shares == D("16") and sell.buy_amount_usd is None


@pytest.mark.parametrize(
    ("decision", "outcome", "message"),
    [
        (_decision(allowed=False), "Up", "not allowed"),
        (_decision(timestamp_ms=NOW - 3_001), "Up", "expired"),
        (_decision(token_id=DOWN), "Up", "does not belong"),
        (_decision(token_id="999"), "Up", "does not belong"),
    ],
)
async def test_refuses_invalid_decisions(
    env: Env, decision: RiskDecision, outcome: str, message: str
) -> None:
    with pytest.raises(ExecutionError, match=message):
        await env.engine.execute(decision, make_market(), outcome)
    assert env.venue.submitted == []
    assert env.store.load_orders() == []


async def test_decision_cannot_be_executed_twice(env: Env) -> None:
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    env.engine.apply_events([_update(rec, OrderStatus.CANCELLED, "0")])
    with pytest.raises(ExecutionError, match="already executed"):
        await env.engine.execute(_decision(), make_market(), "Up")
    assert len(env.venue.submitted) == 1


async def test_decision_reuse_blocked_after_restart(env: Env) -> None:
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    env.engine.apply_events([_update(rec, OrderStatus.CANCELLED, "0")])
    restarted = env.new_engine()
    restarted.load_open_orders()
    with pytest.raises(ExecutionError, match="already executed"):
        await restarted.execute(_decision(), make_market(), "Up")


async def test_max_in_flight_orders(env: Env) -> None:
    await env.engine.execute(_decision(), make_market(), "Up")
    with pytest.raises(ExecutionError, match="in-flight"):
        await env.engine.execute(_decision(decision_id="rd-2"), make_market(), "Up")


async def test_rejection_is_terminal(env: Env) -> None:
    env.venue.mode = "reject"
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    assert rec.status is OrderStatus.REJECTED
    assert rec.last_error and "invalid" in rec.last_error
    assert env.engine.open_orders() == []


@pytest.mark.parametrize("mode", ["hang", "raise", "ambiguous"])
async def test_uncertain_submission_becomes_unknown_and_is_never_resubmitted(
    env: Env, mode: str
) -> None:
    env.venue.mode = mode
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    assert rec.status is OrderStatus.UNKNOWN
    assert env.engine.unknown_count() == 1
    env.venue.mode = "ack"
    with pytest.raises(ExecutionError):
        await env.engine.execute(_decision(decision_id="rd-2"), make_market(), "Up")
    await env.engine.resolve_unknown()
    assert len(env.venue.submitted) == 1  # exactly one submission, ever


async def test_unknown_resolved_from_venue_history(env: Env) -> None:
    env.venue.mode = "raise"
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    # The venue actually received and filled it.
    rec_with_id = replace(rec, exchange_order_id="x-venue")
    env.venue.history = [
        _fill(rec_with_id, 0, shares="16"),
        _update(rec_with_id, OrderStatus.FILLED, "16"),
    ]
    assert await env.engine.resolve_unknown() == []
    final = env.engine.orders[rec.intent.intent_id]
    assert final.status is OrderStatus.FILLED
    assert final.filled_shares == D("16")
    assert len(env.fills) == 1
    assert env.engine.unknown_count() == 0


async def test_unknown_order_stays_blocked_until_timeout_then_escalates(env: Env) -> None:
    env.venue.mode = "raise"
    env.venue.find_raises = True
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    assert await env.engine.resolve_unknown() == []
    env.clock.advance_to(NOW + int(env.cfg.unknown_state_timeout_s * 1000) + 1)
    assert await env.engine.resolve_unknown() == [rec.intent.intent_id]
    assert env.engine.unknown_count() == 1  # still blocked; the operator decides
    env.engine.mark_resolved_no_fill(rec.intent.intent_id, "operator: not on venue")
    assert env.engine.unknown_count() == 0


async def test_open_orders_become_unknown_after_restart(env: Env) -> None:
    await env.engine.execute(_decision(), make_market(), "Up")
    restarted = env.new_engine()
    assert restarted.load_open_orders() == 1
    (rec,) = restarted.open_orders()
    assert rec.status is OrderStatus.UNKNOWN
    assert env.store.load_orders()[0].status is OrderStatus.UNKNOWN


# ---------------------------------------------------------------- fills & invariants
async def test_fills_are_idempotent(env: Env) -> None:
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    f = _fill(rec)
    env.engine.apply_events([f, f])
    env.engine.apply_events([f])
    assert len(env.fills) == 1
    assert env.engine.orders[rec.intent.intent_id].filled_shares == D("5")
    assert env.engine.violations == []


async def test_partial_fill_then_fak_cancel(env: Env) -> None:
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    env.engine.apply_events([_fill(rec)])
    assert env.engine.orders[rec.intent.intent_id].status is OrderStatus.PARTIALLY_FILLED
    assert env.engine.pending_buy_usd() == D("10") - D("0.62") * 5
    env.engine.apply_events([_update(rec, OrderStatus.CANCELLED, "5")])
    final = env.engine.orders[rec.intent.intent_id]
    assert final.status is OrderStatus.CANCELLED
    assert final.fees_usd == D("0.08")
    assert env.engine.open_orders() == []
    assert env.engine.violations == []


async def test_fill_for_unknown_order_is_a_violation(env: Env) -> None:
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    stranger = _fill(rec, intent_id="oi-other", exchange_order_id="x-other")
    env.engine.apply_events([stranger])
    assert env.fills == []
    assert any("did not submit" in v for v in env.engine.violations)


async def test_fill_beyond_limit_is_a_violation(env: Env) -> None:
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    env.engine.apply_events([_fill(rec, price="0.63")])
    assert any("beyond limit" in v for v in env.engine.violations)


async def test_overfill_is_a_violation(env: Env) -> None:
    rec = await env.engine.execute(_exit_decision(), make_market(), "Up")
    env.engine.apply_events([_fill(rec, 0, price="0.60", shares="10")])
    env.engine.apply_events([_fill(rec, 1, price="0.60", shares="10")])
    assert any("overfill" in v for v in env.engine.violations)


async def test_buy_overspend_is_a_violation(env: Env) -> None:
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    env.engine.apply_events([_fill(rec, 0, shares="17")])  # 17 * 0.62 = 10.54 > 10
    assert any("overfill" in v for v in env.engine.violations)


async def test_terminal_update_with_missing_fill_is_a_violation(env: Env) -> None:
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    env.engine.apply_events([_update(rec, OrderStatus.FILLED, "16")])
    assert any("terminal with" in v for v in env.engine.violations)


async def test_update_for_unknown_order_is_a_violation(env: Env) -> None:
    env.engine.apply_events([OrderUpdate("x-ghost", None, OrderStatus.LIVE, D(0), NOW)])
    assert any("unknown order" in v for v in env.engine.violations)


async def test_audit_chain_verifies_after_execution(env: Env) -> None:
    rec = await env.engine.execute(_decision(), make_market(), "Up")
    env.engine.apply_events([_fill(rec), _update(rec, OrderStatus.CANCELLED, "5")])
    ok, count, _ = verify_audit_log(env.audit.path)
    assert ok and count >= 4
