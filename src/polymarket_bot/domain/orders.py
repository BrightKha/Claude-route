"""Order lifecycle objects."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polymarket_bot.domain.types import OrderPurpose, OrderStatus, OrderType, Side

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """An order the Execution Engine intends to submit.

    BUY orders are expressed as a maximum collateral amount (``buy_amount_usd``)
    plus a maximum price; SELL orders as shares plus a minimum price. This
    mirrors Polymarket market-order semantics (docs/research.md §2).
    """

    intent_id: str
    decision_id: str
    condition_id: str
    market_slug: str
    token_id: str
    outcome: str
    side: Side
    order_type: OrderType
    limit_price: Decimal
    buy_amount_usd: Decimal | None
    sell_shares: Decimal | None
    purpose: OrderPurpose
    created_ms: int

    def __post_init__(self) -> None:
        if not (ZERO < self.limit_price < Decimal(1)):
            raise ValueError(f"limit price must be in (0, 1): {self.limit_price}")
        if self.side is Side.BUY:
            if self.buy_amount_usd is None or self.buy_amount_usd <= 0:
                raise ValueError("BUY intent requires positive buy_amount_usd")
            if self.sell_shares is not None:
                raise ValueError("BUY intent must not set sell_shares")
        else:
            if self.sell_shares is None or self.sell_shares <= 0:
                raise ValueError("SELL intent requires positive sell_shares")
            if self.buy_amount_usd is not None:
                raise ValueError("SELL intent must not set buy_amount_usd")
        if self.purpose is OrderPurpose.ENTRY and self.side is not Side.BUY:
            raise ValueError("entries are BUY-only (no naked selling)")


@dataclass(frozen=True, slots=True)
class OrderAck:
    """Immediate result of a submission."""

    intent_id: str
    accepted: bool
    status: OrderStatus
    exchange_order_id: str | None
    filled_shares: Decimal
    avg_price: Decimal | None
    fee_usd: Decimal
    received_ms: int
    error_code: str | None = None
    error_message: str | None = None
    ambiguous: bool = False  # True when we cannot tell whether the exchange accepted it


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    intent_id: str | None
    exchange_order_id: str | None
    condition_id: str
    token_id: str
    outcome: str
    side: Side
    price: Decimal
    shares: Decimal
    fee_usd: Decimal
    ts_ms: int
    liquidity: str  # "taker" | "maker"
    source: str  # "paper" | "live" | "replay"

    def __post_init__(self) -> None:
        if self.shares <= 0:
            raise ValueError("fill shares must be positive")
        if not (ZERO < self.price < Decimal(1)):
            raise ValueError(f"fill price out of range: {self.price}")
        if self.fee_usd < 0:
            raise ValueError("fee cannot be negative")

    @property
    def notional_usd(self) -> Decimal:
        return self.price * self.shares


@dataclass(frozen=True, slots=True)
class OrderRecord:
    """Locally tracked state of an order (persisted before submission)."""

    intent: OrderIntent
    status: OrderStatus
    exchange_order_id: str | None
    filled_shares: Decimal
    filled_notional_usd: Decimal
    fees_usd: Decimal
    updated_ms: int
    last_error: str | None = None

    @property
    def avg_price(self) -> Decimal | None:
        if self.filled_shares <= 0:
            return None
        return self.filled_notional_usd / self.filled_shares


@dataclass(frozen=True, slots=True)
class OrderUpdate:
    """Asynchronous status change reported by an exchange (paper or live)."""

    exchange_order_id: str
    intent_id: str | None
    status: OrderStatus
    cumulative_filled_shares: Decimal
    ts_ms: int
    detail: str = ""


ExecutionEvent = Fill | OrderUpdate


@dataclass(frozen=True, slots=True)
class OpenOrderView:
    """An open order as reported by the exchange (for reconciliation)."""

    exchange_order_id: str
    token_id: str
    side: Side
    price: Decimal
    original_size: Decimal
    size_matched: Decimal
    status: str


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """What the exchange says we own. Source of truth for reconciliation."""

    collateral_usd: Decimal
    positions: dict[str, Decimal]  # token_id -> shares
    open_orders: tuple[OpenOrderView, ...]
    fetched_ms: int
    source: str
    complete: bool = True  # False when any page/field failed to load
