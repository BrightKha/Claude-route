"""Execution Engine: deterministic, idempotent, never retries blindly.

Flow for one approved RiskDecision:

1. refuse if the decision is not allowed, expired, or already executed;
2. build the OrderIntent strictly inside the decision's limits (price rounded
   *towards safety* onto the tick grid);
3. persist the intent (write-ahead) and audit it;
4. submit once, with a timeout. A timeout/transport error makes the order
   UNKNOWN — it is **never** resubmitted. The unknown state is resolved by
   querying the venue (``find_orders_since``) and, failing that, the bot halts
   for the operator;
5. apply fills idempotently (by fill id) and enforce invariants: no overfill,
   no fill beyond the limit price, no fill for an order we do not know.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

from polymarket_bot.audit.audit_log import AuditLog
from polymarket_bot.config.app_config import ExecutionConfig
from polymarket_bot.domain.clock import Clock
from polymarket_bot.domain.decisions import RiskDecision
from polymarket_bot.domain.market import MarketDefinition
from polymarket_bot.domain.orders import (
    ExecutionEvent,
    Fill,
    OrderIntent,
    OrderRecord,
    OrderUpdate,
)
from polymarket_bot.domain.types import OrderPurpose, OrderStatus, OrderType, Side
from polymarket_bot.ports import TradingProvider
from polymarket_bot.risk.engine import ceil_to_tick, floor_to_tick
from polymarket_bot.storage.sqlite_store import StateStore

log = logging.getLogger(__name__)

ZERO = Decimal(0)
NOTIONAL_TOLERANCE = Decimal("0.01")
MAX_DECISION_AGE_MS = 3_000


class ExecutionError(RuntimeError):
    pass


class InvariantViolationError(RuntimeError):
    """Something that must never happen happened. Callers engage the kill switch."""


FillCallback = Callable[[Fill, OrderRecord], None]


class ExecutionEngine:
    def __init__(
        self,
        provider: TradingProvider,
        store: StateStore,
        audit: AuditLog,
        clock: Clock,
        config: ExecutionConfig,
        *,
        on_fill: FillCallback,
    ) -> None:
        self._provider = provider
        self._store = store
        self._audit = audit
        self._clock = clock
        self._cfg = config
        self._on_fill = on_fill
        self.orders: dict[str, OrderRecord] = {}
        self._by_exchange_id: dict[str, str] = {}
        self._used_decisions: set[str] = set()
        self.violations: list[str] = []

    # ------------------------------------------------------------------ startup
    def load_open_orders(self) -> int:
        """Orders open at the last shutdown are UNKNOWN until reconciled (fail closed)."""
        count = 0
        for rec in self._store.load_orders(only_open=True):
            unknown = replace(
                rec,
                status=OrderStatus.UNKNOWN,
                updated_ms=self._clock.now_ms(),
                last_error="open at restart; must be reconciled",
            )
            self._store.update_order(unknown)
            self.orders[rec.intent.intent_id] = unknown
            if rec.exchange_order_id:
                self._by_exchange_id[rec.exchange_order_id] = rec.intent.intent_id
            count += 1
        for rec in self._store.load_orders():
            self._used_decisions.add(rec.intent.decision_id)
            if rec.exchange_order_id:
                self._by_exchange_id.setdefault(rec.exchange_order_id, rec.intent.intent_id)
        return count

    # ------------------------------------------------------------------ views
    def open_orders(self) -> list[OrderRecord]:
        return [r for r in self.orders.values() if r.status.is_open]

    def unknown_count(self) -> int:
        return sum(1 for r in self.orders.values() if r.status is OrderStatus.UNKNOWN)

    def in_flight_tokens(self) -> frozenset[str]:
        return frozenset(r.intent.token_id for r in self.open_orders())

    def pending_buy_usd(self) -> Decimal:
        total = ZERO
        for r in self.open_orders():
            if r.intent.side is Side.BUY and r.intent.buy_amount_usd is not None:
                total += max(ZERO, r.intent.buy_amount_usd - r.filled_notional_usd)
        return total

    def pending_sell_shares(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for r in self.open_orders():
            if r.intent.side is Side.SELL and r.intent.sell_shares is not None:
                left = max(ZERO, r.intent.sell_shares - r.filled_shares)
                out[r.intent.token_id] = out.get(r.intent.token_id, ZERO) + left
        return out

    def known_exchange_ids(self) -> frozenset[str]:
        return frozenset(self._by_exchange_id)

    # ------------------------------------------------------------------ submission
    def build_intent(
        self, decision: RiskDecision, market: MarketDefinition, outcome: str
    ) -> OrderIntent:
        now = self._clock.now_ms()
        if not decision.allowed:
            raise ExecutionError(f"decision {decision.decision_id} is not allowed")
        if now - decision.timestamp_ms > MAX_DECISION_AGE_MS:
            raise ExecutionError(f"decision {decision.decision_id} expired")
        if decision.decision_id in self._used_decisions:
            raise ExecutionError(f"decision {decision.decision_id} already executed")
        if (
            decision.token_id not in market.token_ids
            or market.outcome_of(decision.token_id) != outcome
        ):
            raise ExecutionError("decision token does not belong to the market/outcome")
        tick = market.tick_size
        if decision.side is Side.BUY:
            limit = floor_to_tick(decision.limit_price, tick)  # never above the approved max
            order_type = OrderType(self._cfg.entry_order_type)
            amount: Decimal | None = decision.max_size_usd
            shares: Decimal | None = None
        else:
            limit = ceil_to_tick(decision.limit_price, tick)  # never below the approved min
            order_type = OrderType(self._cfg.exit_order_type)
            amount, shares = None, decision.max_shares
        return OrderIntent(
            intent_id=f"oi-{uuid.uuid4().hex}",
            decision_id=decision.decision_id,
            condition_id=market.condition_id,
            market_slug=market.slug,
            token_id=decision.token_id,
            outcome=outcome,
            side=decision.side,
            order_type=order_type,
            limit_price=limit,
            buy_amount_usd=amount,
            sell_shares=shares,
            purpose=decision.purpose,
            created_ms=now,
        )

    async def execute(
        self, decision: RiskDecision, market: MarketDefinition, outcome: str
    ) -> OrderRecord:
        if len(self.open_orders()) >= self._cfg.max_in_flight_orders:
            raise ExecutionError("max in-flight orders reached")
        if self.unknown_count() > 0:
            raise ExecutionError("an order is in UNKNOWN state; no new orders")
        intent = self.build_intent(decision, market, outcome)
        # Write-ahead: persisted before submission; UNIQUE(decision_id) guards duplicates.
        self._store.insert_order_intent(intent)
        self._used_decisions.add(decision.decision_id)
        record = OrderRecord(
            intent, OrderStatus.SUBMITTING, None, ZERO, ZERO, ZERO, intent.created_ms
        )
        self.orders[intent.intent_id] = record
        self._store.update_order(record)
        self._audit.append("order_submit", {"intent": intent, "decision_id": decision.decision_id})
        try:
            ack = await asyncio.wait_for(
                self._provider.submit(intent, market), timeout=self._cfg.submit_timeout_s
            )
        except (TimeoutError, OSError, ConnectionError) as exc:
            record = replace(
                record,
                status=OrderStatus.UNKNOWN,
                updated_ms=self._clock.now_ms(),
                last_error=f"submission outcome unknown: {type(exc).__name__}",
            )
            return self._save(record, "order_unknown")
        except Exception as exc:  # any other failure before an ack is also ambiguous
            record = replace(
                record,
                status=OrderStatus.UNKNOWN,
                updated_ms=self._clock.now_ms(),
                last_error=f"submission error: {type(exc).__name__}",
            )
            return self._save(record, "order_unknown")
        if ack.ambiguous:
            record = replace(
                record,
                status=OrderStatus.UNKNOWN,
                updated_ms=ack.received_ms,
                exchange_order_id=ack.exchange_order_id,
                last_error="ambiguous ack",
            )
        elif not ack.accepted:
            record = replace(
                record,
                status=OrderStatus.REJECTED,
                updated_ms=ack.received_ms,
                last_error=f"{ack.error_code}: {ack.error_message}",
            )
        else:
            record = replace(
                record,
                status=ack.status,
                exchange_order_id=ack.exchange_order_id,
                updated_ms=ack.received_ms,
            )
        if record.exchange_order_id:
            self._by_exchange_id[record.exchange_order_id] = intent.intent_id
        return self._save(record, "order_ack")

    # ------------------------------------------------------------------ events
    def apply_events(self, events: list[ExecutionEvent]) -> None:
        for ev in events:
            if isinstance(ev, Fill):
                self._apply_fill(ev)
            else:
                self._apply_update(ev)

    def _lookup(self, intent_id: str | None, exchange_id: str | None) -> OrderRecord | None:
        if intent_id and intent_id in self.orders:
            return self.orders[intent_id]
        if exchange_id and exchange_id in self._by_exchange_id:
            return self.orders.get(self._by_exchange_id[exchange_id])
        return None

    def _apply_fill(self, fill: Fill) -> None:
        rec = self._lookup(fill.intent_id, fill.exchange_order_id)
        if rec is None:
            self._violation(f"fill {fill.fill_id} for an order the bot did not submit")
            return
        if not self._store.insert_fill(fill):
            return  # duplicate event: already applied
        intent = rec.intent
        if fill.side is not intent.side or fill.token_id != intent.token_id:
            self._violation(f"fill {fill.fill_id} side/token mismatch")
            return
        if (intent.side is Side.BUY and fill.price > intent.limit_price) or (
            intent.side is Side.SELL and fill.price < intent.limit_price
        ):
            self._violation(
                f"fill {fill.fill_id} at {fill.price} beyond limit {intent.limit_price}"
            )
        shares = rec.filled_shares + fill.shares
        notional = rec.filled_notional_usd + fill.price * fill.shares
        if (
            intent.side is Side.SELL
            and intent.sell_shares is not None
            and shares > intent.sell_shares
        ):
            self._violation(f"overfill: sold {shares} > {intent.sell_shares}")
        if intent.side is Side.BUY and intent.buy_amount_usd is not None:
            if notional > intent.buy_amount_usd + NOTIONAL_TOLERANCE:
                self._violation(f"overfill: spent {notional} > {intent.buy_amount_usd}")
        status = (
            rec.status if rec.status is not OrderStatus.UNKNOWN else OrderStatus.PARTIALLY_FILLED
        )
        if status in (OrderStatus.LIVE, OrderStatus.SUBMITTING):
            status = OrderStatus.PARTIALLY_FILLED
        rec = replace(
            rec,
            filled_shares=shares,
            filled_notional_usd=notional,
            fees_usd=rec.fees_usd + fill.fee_usd,
            updated_ms=fill.ts_ms,
            status=status,
        )
        self._save(rec, "fill", extra={"fill": fill})
        self._on_fill(fill, rec)

    def _apply_update(self, upd: OrderUpdate) -> None:
        rec = self._lookup(upd.intent_id, upd.exchange_order_id)
        if rec is None:
            self._violation(f"update for unknown order {upd.exchange_order_id}")
            return
        if rec.status.is_terminal:
            return
        if upd.status.is_terminal and upd.cumulative_filled_shares != rec.filled_shares:
            # Fills arrive before the terminal update; a mismatch means we missed one.
            self._violation(
                f"order {upd.exchange_order_id} terminal with {upd.cumulative_filled_shares} "
                f"filled but {rec.filled_shares} applied"
            )
        status = upd.status
        if status is OrderStatus.CANCELLED and rec.filled_shares > 0:
            status = OrderStatus.FILLED if _fully_filled(rec) else OrderStatus.CANCELLED
        rec = replace(
            rec,
            status=status,
            updated_ms=upd.ts_ms,
            exchange_order_id=rec.exchange_order_id or upd.exchange_order_id,
        )
        self._save(rec, "order_update", extra={"detail": upd.detail})

    async def resolve_unknown(self) -> list[str]:
        """Query the venue for UNKNOWN orders. Returns ids still unresolved past the timeout."""
        now = self._clock.now_ms()
        stuck: list[str] = []
        for rec in [r for r in self.orders.values() if r.status is OrderStatus.UNKNOWN]:
            try:
                events = await self._provider.find_orders_since(
                    rec.intent.token_id, rec.intent.created_ms
                )
            except Exception:  # venue unreachable: stays unknown
                log.exception("resolve_unknown query failed")
                events = []
            mine = [
                e
                for e in events
                if (e.intent_id == rec.intent.intent_id)
                or (rec.exchange_order_id and e.exchange_order_id == rec.exchange_order_id)
            ]
            if mine:
                self.apply_events(mine)
                continue
            if now - rec.updated_ms > self._cfg.unknown_state_timeout_s * 1000:
                stuck.append(rec.intent.intent_id)
        return stuck

    def mark_resolved_no_fill(self, intent_id: str, reason: str) -> None:
        """Operator/reconciliation confirmed the order never reached the book."""
        rec = self.orders[intent_id]
        self._save(
            replace(
                rec,
                status=OrderStatus.CANCELLED,
                updated_ms=self._clock.now_ms(),
                last_error=reason,
            ),
            "order_resolved",
        )

    # ------------------------------------------------------------------ helpers
    def _save(
        self, rec: OrderRecord, kind: str, extra: dict[str, object] | None = None
    ) -> OrderRecord:
        self.orders[rec.intent.intent_id] = rec
        self._store.update_order(rec)
        self._audit.append(kind, {"order": rec, **(extra or {})})
        return rec

    def _violation(self, message: str) -> None:
        log.critical("EXECUTION INVARIANT VIOLATION: %s", message)
        self.violations.append(message)
        self._audit.append("invariant_violation", {"message": message})

    def record_purpose(self, intent_id: str) -> OrderPurpose:
        return self.orders[intent_id].intent.purpose


def _fully_filled(rec: OrderRecord) -> bool:
    intent = rec.intent
    if intent.side is Side.SELL and intent.sell_shares is not None:
        return rec.filled_shares >= intent.sell_shares
    if intent.buy_amount_usd is not None:
        return intent.buy_amount_usd - rec.filled_notional_usd < intent.limit_price * Decimal(
            "0.01"
        )
    return False
