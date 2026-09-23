"""Incremental order book per outcome token, with integrity checks.

A book is only *valid* after a full snapshot (WS ``book`` event or REST
``/book``). It becomes invalid — and therefore unusable for trading — on:
disconnect, crossed book, out-of-order update, malformed delta, or a mismatch
between our top of book and the ``best_bid``/``best_ask`` echoed by the
exchange in ``price_change`` events.
"""

from __future__ import annotations

from decimal import Decimal

from polymarket_bot.domain.market import ALLOWED_TICK_SIZES, BookLevel, OrderBookSnapshot

ZERO = Decimal(0)


class OrderBookState:
    def __init__(self, token_id: str, *, max_out_of_order_ms: int = 0) -> None:
        self.token_id = token_id
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self.valid = False
        self.invalid_reason = "no snapshot yet"
        self.received_ms: int | None = None
        self.exchange_ms: int | None = None
        self.book_hash: str | None = None
        self.tick_size: Decimal | None = None
        self.last_trade_price: Decimal | None = None
        self.sequence = 0
        self._max_ooo = max_out_of_order_ms

    # ------------------------------------------------------------------ mutation
    def invalidate(self, reason: str) -> None:
        self.valid = False
        self.invalid_reason = reason

    def apply_snapshot(
        self,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
        *,
        received_ms: int,
        exchange_ms: int | None,
        book_hash: str | None,
        tick_size: Decimal | None,
    ) -> None:
        if exchange_ms is not None and self.exchange_ms is not None and self.valid:
            if exchange_ms + self._max_ooo < self.exchange_ms:
                return  # older snapshot than what we already have; ignore it
        for price, size in (*bids, *asks):
            if not (ZERO < price < Decimal(1)) or size < 0:
                self.invalidate(f"malformed snapshot level {price}/{size}")
                return
        self._bids = {p: s for p, s in bids if s > 0}
        self._asks = {p: s for p, s in asks if s > 0}
        self.received_ms = received_ms
        self.exchange_ms = exchange_ms
        self.book_hash = book_hash
        if tick_size is not None:
            self.set_tick_size(tick_size)
        self.sequence += 1
        self.valid = True
        self.invalid_reason = ""
        self.check_crossed()

    def apply_level(
        self,
        side: str,
        price: Decimal,
        size: Decimal,
        *,
        received_ms: int,
        exchange_ms: int | None,
        check_crossed: bool = True,
    ) -> None:
        """Apply one level. Multi-level events pass ``check_crossed=False`` and call
        :meth:`check_crossed` once after the whole event: a batch may legitimately
        pass through a transiently crossed state (e.g. the book moving up a tick)."""
        if not self.valid:
            return  # deltas on an invalid book are meaningless; wait for a snapshot
        if exchange_ms is not None and self.exchange_ms is not None:
            if exchange_ms + self._max_ooo < self.exchange_ms:
                self.invalidate(f"out-of-order update {exchange_ms} < {self.exchange_ms}")
                return
        if not (ZERO < price < Decimal(1)) or size < 0:
            self.invalidate(f"malformed level {side} {price}/{size}")
            return
        book = self._bids if side == "BUY" else self._asks if side == "SELL" else None
        if book is None:
            self.invalidate(f"unknown side {side!r}")
            return
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.received_ms = received_ms
        if exchange_ms is not None:
            self.exchange_ms = max(exchange_ms, self.exchange_ms or exchange_ms)
        self.sequence += 1
        if check_crossed:
            self.check_crossed()

    def verify_top(self, best_bid: Decimal | None, best_ask: Decimal | None) -> None:
        """Cross-check our top of book against the exchange's echo."""
        if not self.valid:
            return
        ours_bid = max(self._bids) if self._bids else None
        ours_ask = min(self._asks) if self._asks else None
        if best_bid is not None and best_bid > 0 and ours_bid != best_bid:
            self.invalidate(f"best bid mismatch ours={ours_bid} exchange={best_bid}")
        elif best_ask is not None and best_ask < 1 and ours_ask != best_ask:
            self.invalidate(f"best ask mismatch ours={ours_ask} exchange={best_ask}")

    def set_tick_size(self, tick: Decimal) -> None:
        if tick not in ALLOWED_TICK_SIZES:
            self.invalidate(f"unsupported tick size {tick}")
            return
        self.tick_size = tick

    def check_crossed(self) -> None:
        if not self.valid:
            return
        if self._bids and self._asks and max(self._bids) >= min(self._asks):
            self.invalidate(f"crossed book bid={max(self._bids)} ask={min(self._asks)}")

    # ------------------------------------------------------------------ views
    def snapshot(self) -> OrderBookSnapshot | None:
        if not self.valid or self.received_ms is None:
            return None
        return OrderBookSnapshot(
            token_id=self.token_id,
            bids=tuple(BookLevel(p, s) for p, s in sorted(self._bids.items(), reverse=True)),
            asks=tuple(BookLevel(p, s) for p, s in sorted(self._asks.items())),
            received_ms=self.received_ms,
            exchange_ms=self.exchange_ms,
            book_hash=self.book_hash,
            tick_size=self.tick_size,
            last_trade_price=self.last_trade_price,
            sequence=self.sequence,
        )
