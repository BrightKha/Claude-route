"""Market definitions, fee schedule, order book snapshots. Pure, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

# Tick sizes accepted by Polymarket CLOB V2 (SDK `_ALLOWED_TICK_SIZES`, docs/research.md §2).
ALLOWED_TICK_SIZES: frozenset[Decimal] = frozenset(
    Decimal(x) for x in ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001")
)
# Fees are rounded to 5 decimals by Polymarket (docs /trading/fees "Fee Precision").
FEE_QUANTUM = Decimal("0.00001")
ONE = Decimal(1)
ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Taker fee schedule: ``fee = shares * rate * (p * (1 - p)) ** exponent``.

    Source: docs.polymarket.com/trading/fees and SDK ``adjust_buy_amount_for_fees``.
    """

    rate: Decimal
    exponent: Decimal
    taker_only: bool = True

    def __post_init__(self) -> None:
        if self.rate < 0 or self.rate > 1:
            raise ValueError(f"fee rate out of range: {self.rate}")
        if self.exponent < 0 or self.exponent > 4:
            raise ValueError(f"fee exponent out of range: {self.exponent}")

    def fee_rate_at(self, price: Decimal) -> Decimal:
        """Effective fee per share (in USDC) at ``price``."""
        if price <= 0 or price >= 1:
            return ZERO
        base = price * (ONE - price)
        return self.rate * (base**self.exponent if self.exponent != 1 else base)

    def taker_fee(self, shares: Decimal, price: Decimal, *, conservative: bool = True) -> Decimal:
        """Fee in USDC for a taker fill of ``shares`` at ``price``.

        ``conservative=True`` rounds *up* to the fee quantum (worst case for us);
        the exact exchange rounding mode is not documented.
        """
        if shares < 0:
            raise ValueError("shares must be non-negative")
        raw = shares * self.fee_rate_at(price)
        rounding = ROUND_CEILING if conservative else ROUND_FLOOR
        return raw.quantize(FEE_QUANTUM, rounding=rounding)


@dataclass(frozen=True, slots=True)
class OutcomeToken:
    token_id: str
    outcome: str  # label as published by Polymarket, e.g. "Up" / "Down"


@dataclass(frozen=True, slots=True)
class MarketDefinition:
    """A validated, tradable binary market.

    Instances are only created by a resolution adapter after the market text,
    resolution source and configuration matched a registered rule version.
    """

    market_id: str
    condition_id: str
    slug: str
    question: str
    tokens: tuple[OutcomeToken, OutcomeToken]
    window_start_ms: int
    window_end_ms: int
    tick_size: Decimal
    min_order_size: Decimal
    fee_schedule: FeeSchedule
    neg_risk: bool
    rule_id: str
    accepting_orders: bool
    description_sha256: str

    def __post_init__(self) -> None:
        if self.tick_size not in ALLOWED_TICK_SIZES:
            raise ValueError(f"unsupported tick size {self.tick_size}")
        if self.window_end_ms <= self.window_start_ms:
            raise ValueError("window_end must be after window_start")
        if self.tokens[0].token_id == self.tokens[1].token_id:
            raise ValueError("outcome tokens must differ")
        if self.min_order_size <= 0:
            raise ValueError("min_order_size must be positive")

    def token(self, outcome: str) -> OutcomeToken:
        for tok in self.tokens:
            if tok.outcome == outcome:
                return tok
        raise KeyError(f"unknown outcome {outcome!r} for {self.slug}")

    def outcome_of(self, token_id: str) -> str:
        for tok in self.tokens:
            if tok.token_id == token_id:
                return tok.outcome
        raise KeyError(f"token {token_id} not in market {self.slug}")

    def other(self, outcome: str) -> OutcomeToken:
        for tok in self.tokens:
            if tok.outcome != outcome:
                return tok
        raise KeyError(outcome)

    @property
    def token_ids(self) -> tuple[str, str]:
        return (self.tokens[0].token_id, self.tokens[1].token_id)


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    """Immutable view of one outcome token's book.

    ``bids`` are sorted best (highest) first, ``asks`` best (lowest) first.
    """

    token_id: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    received_ms: int
    exchange_ms: int | None
    book_hash: str | None
    tick_size: Decimal | None
    last_trade_price: Decimal | None = None
    sequence: int = 0
    notes: tuple[str, ...] = field(default=())

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def is_crossed(self) -> bool:
        return (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid >= self.best_ask
        )

    def depth_usd(self, side: str, within: Decimal) -> Decimal:
        """Notional available within ``within`` (price distance) of the best level."""
        levels = self.asks if side == "ask" else self.bids
        if not levels:
            return ZERO
        best = levels[0].price
        total = ZERO
        for lvl in levels:
            if abs(lvl.price - best) > within:
                break
            total += lvl.price * lvl.size
        return total

    def imbalance(self, levels: int = 5) -> Decimal | None:
        """(bid_size - ask_size) / (bid_size + ask_size) over the top ``levels``."""
        bid = sum((lvl.size for lvl in self.bids[:levels]), ZERO)
        ask = sum((lvl.size for lvl in self.asks[:levels]), ZERO)
        if bid + ask == 0:
            return None
        return (bid - ask) / (bid + ask)
