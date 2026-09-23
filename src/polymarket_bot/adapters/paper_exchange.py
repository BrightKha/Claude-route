"""Paper exchange: realistic simulated execution against the *observed* book.

Realism rules:
* an order submitted at ``t`` becomes matchable at ``t + latency + jitter +
  taker_delay`` (Polymarket crypto taker delay, docs/research.md §2) and is
  matched against the book as observed at that later time — never earlier
  (no lookahead) and never at the mid;
* FAK fills what the book offers up to the limit, then cancels the rest;
  FOK fills entirely or not at all; GTC/GTD are refused (live policy D4);
* our own fills consume displayed liquidity until the next book update;
* taker fees use the market's official fee schedule, rounded conservatively;
* the exchange keeps its *own* ledger (cash, positions), independent from the
  bot's portfolio, so reconciliation is meaningful in paper mode too.

This is a ``Paper*`` component: it never talks to Polymarket.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from polymarket_bot.config.app_config import PaperExchangeConfig
from polymarket_bot.domain.clock import Clock
from polymarket_bot.domain.market import FeeSchedule, MarketDefinition, OrderBookSnapshot
from polymarket_bot.domain.orders import (
    AccountSnapshot,
    ExecutionEvent,
    Fill,
    OrderAck,
    OrderIntent,
    OrderUpdate,
)
from polymarket_bot.domain.types import OrderStatus, OrderType, Side
from polymarket_bot.ports import CancelResult

ZERO = Decimal(0)
SHARE_Q = Decimal("0.01")

BookSource = Callable[[str], OrderBookSnapshot | None]


@dataclass
class _Pending:
    intent: OrderIntent
    order_id: str
    due_ms: int
    fees: FeeSchedule
    tick: Decimal


class PaperExchange:
    name = "paper"

    def __init__(
        self,
        config: PaperExchangeConfig,
        clock: Clock,
        book_source: BookSource,
        *,
        source: str = "paper",
    ) -> None:
        self._cfg = config
        self._clock = clock
        self._books = book_source
        self._rng = random.Random(config.seed)  # noqa: S311 - simulation only
        self._source = source
        self.cash_usd = config.initial_balance_usd
        self.positions: dict[str, Decimal] = {}
        self._pending: list[_Pending] = []
        self._events: list[ExecutionEvent] = []
        self._history: list[tuple[str, ExecutionEvent]] = []  # (token_id, event)
        # (token, side, price) -> [(ts_ms, size)] of our own simulated consumption
        self._consumed: dict[tuple[str, str, Decimal], list[tuple[int, Decimal]]] = {}
        self.submitted = 0

    # ------------------------------------------------------------------ TradingProvider
    async def submit(self, intent: OrderIntent, market: MarketDefinition) -> OrderAck:
        now = self._clock.now_ms()
        self.submitted += 1
        reject = self._validate(intent, market)
        if reject is None and self._cfg.reject_probability > 0:
            if self._rng.random() < self._cfg.reject_probability:
                reject = "chaos: simulated rejection"
        if reject is not None:
            return OrderAck(
                intent.intent_id,
                False,
                OrderStatus.REJECTED,
                None,
                ZERO,
                None,
                ZERO,
                now,
                error_code="rejected",
                error_message=reject,
            )
        order_id = f"paper-{uuid.uuid4().hex[:20]}"
        jitter = (
            self._rng.randint(0, self._cfg.latency_jitter_ms) if self._cfg.latency_jitter_ms else 0
        )
        due = now + self._cfg.latency_ms + jitter + self._cfg.taker_delay_ms
        self._pending.append(_Pending(intent, order_id, due, market.fee_schedule, market.tick_size))
        return OrderAck(intent.intent_id, True, OrderStatus.LIVE, order_id, ZERO, None, ZERO, now)

    def _validate(self, intent: OrderIntent, market: MarketDefinition) -> str | None:
        if intent.order_type not in (OrderType.FAK, OrderType.FOK):
            return f"order type {intent.order_type} not supported by policy"
        if intent.token_id not in market.token_ids:
            return "token not in market"
        if (intent.limit_price / market.tick_size) != (
            intent.limit_price / market.tick_size
        ).to_integral_value():
            return f"limit {intent.limit_price} not on tick {market.tick_size}"
        if intent.side is Side.BUY:
            assert intent.buy_amount_usd is not None
            worst_fee = (
                market.fee_schedule.fee_rate_at(intent.limit_price)
                * self._cfg.fee_multiplier
                / intent.limit_price
            )
            if intent.buy_amount_usd * (1 + worst_fee) > self.cash_usd:
                return "not enough balance"
        else:
            assert intent.sell_shares is not None
            if intent.sell_shares > self.positions.get(intent.token_id, ZERO):
                return "not enough shares"
        return None

    async def cancel(self, exchange_order_id: str) -> CancelResult:
        for p in list(self._pending):
            if p.order_id == exchange_order_id:
                self._pending.remove(p)
                self._emit(
                    p.intent.token_id,
                    OrderUpdate(
                        p.order_id,
                        p.intent.intent_id,
                        OrderStatus.CANCELLED,
                        ZERO,
                        self._clock.now_ms(),
                        "cancelled",
                    ),
                )
                return CancelResult((exchange_order_id,), {}, True)
        return CancelResult((), {exchange_order_id: "not open"}, True)

    async def cancel_all(self) -> CancelResult:
        ids = [p.order_id for p in self._pending]
        for oid in ids:
            await self.cancel(oid)
        return CancelResult(tuple(ids), {}, True)

    async def poll_events(self) -> list[ExecutionEvent]:
        self.advance(self._clock.now_ms())
        events, self._events = self._events, []
        return events

    async def find_orders_since(self, token_id: str, since_ms: int) -> list[ExecutionEvent]:
        out: list[ExecutionEvent] = []
        for tok, ev in self._history:
            ts = ev.ts_ms
            if tok == token_id and ts >= since_ms:
                out.append(ev)
        return out

    # ------------------------------------------------------------------ AccountProvider
    async def account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            collateral_usd=self.cash_usd,
            positions={t: s for t, s in self.positions.items() if s > 0},
            open_orders=(),
            fetched_ms=self._clock.now_ms(),
            source=self._source,
        )

    # ------------------------------------------------------------------ simulation
    def pending_count(self) -> int:
        return len(self._pending)

    def advance(self, now_ms: int) -> None:
        """Match every pending order whose arrival time has passed."""
        due = sorted((p for p in self._pending if p.due_ms <= now_ms), key=lambda p: p.due_ms)
        for p in due:
            self._pending.remove(p)
            # Matched against the current book (the replay engine advances before applying
            # the next event, so this is the book as of the arrival time), stamped at arrival.
            self._match(p, p.due_ms)

    def _available(
        self, book: OrderBookSnapshot, side: str, now_ms: int
    ) -> list[tuple[Decimal, Decimal]]:
        """Displayed size minus our own recent simulated consumption (conservative).

        The real feed never reflects paper fills, so consumed size stays hidden
        for ``liquidity_replenish_ms`` regardless of unrelated book updates.
        """
        horizon = now_ms - self._cfg.liquidity_replenish_ms
        levels = book.asks if side == "ask" else book.bids
        out: list[tuple[Decimal, Decimal]] = []
        for lvl in levels:
            key = (book.token_id, side, lvl.price)
            recent = [(t, s) for t, s in self._consumed.get(key, []) if t > horizon]
            if recent:
                self._consumed[key] = recent
            else:
                self._consumed.pop(key, None)
            used = sum((s for _, s in recent), ZERO)
            out.append((lvl.price, lvl.size - used))
        return out

    def _consume(self, token: str, side: str, price: Decimal, size: Decimal, now_ms: int) -> None:
        self._consumed.setdefault((token, side, price), []).append((now_ms, size))

    def _match(self, p: _Pending, now_ms: int) -> None:
        intent = p.intent
        book = self._books(intent.token_id)
        if book is None:
            self._emit(
                intent.token_id,
                OrderUpdate(
                    p.order_id,
                    intent.intent_id,
                    OrderStatus.CANCELLED,
                    ZERO,
                    now_ms,
                    "no valid book",
                ),
            )
            return
        fills: list[tuple[Decimal, Decimal]] = []
        if intent.side is Side.BUY:
            assert intent.buy_amount_usd is not None
            budget = intent.buy_amount_usd
            for price, size in self._available(book, "ask", now_ms):
                if price > intent.limit_price or budget <= 0:
                    break
                if size <= 0:
                    continue
                take = min(size, (budget / price).quantize(SHARE_Q, rounding=ROUND_FLOOR))
                if take > 0:
                    fills.append((price, take))
                    budget -= take * price
            # Complete when not even one more minimal share lot is affordable.
            complete = bool(fills) and budget < fills[-1][0] * SHARE_Q
        else:
            assert intent.sell_shares is not None
            remaining = intent.sell_shares
            for price, size in self._available(book, "bid", now_ms):
                if price < intent.limit_price or remaining <= 0:
                    break
                if size <= 0:
                    continue
                take = min(size, remaining)
                fills.append((price, take))
                remaining -= take
            complete = remaining <= 0
        if intent.order_type is OrderType.FOK and not complete:
            fills = []
        if not fills:
            self._emit(
                intent.token_id,
                OrderUpdate(
                    p.order_id,
                    intent.intent_id,
                    OrderStatus.CANCELLED,
                    ZERO,
                    now_ms,
                    "no liquidity within limit",
                ),
            )
            return
        cum = ZERO
        for i, (price, size) in enumerate(fills):
            fee = p.fees.taker_fee(size, price) * self._cfg.fee_multiplier
            side_key = "ask" if intent.side is Side.BUY else "bid"
            self._consume(intent.token_id, side_key, price, size, now_ms)
            if intent.side is Side.BUY:
                self.cash_usd -= price * size + fee
                self.positions[intent.token_id] = self.positions.get(intent.token_id, ZERO) + size
            else:
                self.cash_usd += price * size - fee
                self.positions[intent.token_id] = self.positions.get(intent.token_id, ZERO) - size
            cum += size
            self._emit(
                intent.token_id,
                Fill(
                    fill_id=f"{p.order_id}-{i}",
                    intent_id=intent.intent_id,
                    exchange_order_id=p.order_id,
                    condition_id=intent.condition_id,
                    token_id=intent.token_id,
                    outcome=intent.outcome,
                    side=intent.side,
                    price=price,
                    shares=size,
                    fee_usd=fee,
                    ts_ms=now_ms,
                    liquidity="taker",
                    source=self._source,
                ),
            )
        status = (
            OrderStatus.FILLED if complete else OrderStatus.CANCELLED
        )  # FAK remainder cancelled
        self._emit(
            intent.token_id,
            OrderUpdate(p.order_id, intent.intent_id, status, cum, now_ms, "matched"),
        )

    def settle(self, winning_token: str, losing_token: str) -> Decimal:
        """Redeem at resolution: 1 per winning share, 0 per losing share."""
        payout = self.positions.pop(winning_token, ZERO)
        self.positions.pop(losing_token, None)
        self.cash_usd += payout
        return payout

    def _emit(self, token_id: str, event: ExecutionEvent) -> None:
        self._events.append(event)
        self._history.append((token_id, event))
