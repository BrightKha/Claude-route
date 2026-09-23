"""MarketSnapshot: everything the decision pipeline may know at one instant."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polymarket_bot.domain.market import MarketDefinition, OrderBookSnapshot


@dataclass(frozen=True, slots=True)
class TokenQuote:
    token_id: str
    outcome: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid: Decimal | None
    spread: Decimal | None
    bid_depth_usd: Decimal
    ask_depth_usd: Decimal
    last_trade_price: Decimal | None
    imbalance: Decimal | None
    book_received_ms: int | None
    book_exchange_ms: int | None
    book_age_ms: int | None
    book_valid: bool
    book: OrderBookSnapshot | None


@dataclass(frozen=True, slots=True)
class ReferenceView:
    """Reference (resolution-source) prices as known at snapshot time."""

    spot: Decimal | None
    spot_observed_ms: int | None
    spot_age_ms: int | None
    twap: Decimal | None
    twap_observed_ms: int | None
    twap_age_ms: int | None
    price_to_beat: Decimal | None
    price_to_beat_source: str | None
    price_to_beat_verified: bool
    secondary_spot: Decimal | None  # e.g. Binance, for dispersion checks only
    secondary_age_ms: int | None
    dispersion_bps: float | None


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    snapshot_id: str
    monotonic_ns: int
    utc_ms: int
    market: MarketDefinition
    quotes: tuple[TokenQuote, TokenQuote]
    reference: ReferenceView
    time_to_expiry_ms: int
    time_since_start_ms: int
    feeds_connected: bool
    stale_reasons: tuple[str, ...]

    def quote(self, outcome: str) -> TokenQuote:
        for q in self.quotes:
            if q.outcome == outcome:
                return q
        raise KeyError(outcome)

    @property
    def is_fresh(self) -> bool:
        return not self.stale_reasons and self.feeds_connected
