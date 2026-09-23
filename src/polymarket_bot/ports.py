"""Internal interfaces (ports).

Business logic depends only on these protocols. Concrete implementations live
in ``polymarket_bot.adapters``: live Polymarket (SDK), paper, replay and fakes.
Discovery, market data, trading, account and resolution are separate on purpose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from polymarket_bot.domain.market import MarketDefinition
from polymarket_bot.domain.orders import AccountSnapshot, ExecutionEvent, OrderAck, OrderIntent


@dataclass(frozen=True, slots=True)
class RawMessage:
    """A payload exactly as received, stamped with local receive times.

    Raw messages are what the recorder persists and the replay engine re-emits,
    so live and replay share the same normalization code path.
    """

    source: str  # "gamma" | "clob_rest" | "clob_ws" | "rtds" | "user_ws" | ...
    kind: str  # e.g. "events", "book", "ws_frame", "connection"
    payload: Any
    received_ms: int
    monotonic_ns: int
    meta: dict[str, Any] | None = None


class MarketDiscoveryProvider(Protocol):
    async def fetch_series_events(
        self, *, series_id: str, closed: bool, limit: int
    ) -> RawMessage: ...

    async def fetch_events_by_slugs(self, slugs: list[str]) -> RawMessage: ...

    async def fetch_market_by_slug(self, slug: str) -> RawMessage: ...


class MarketDataProvider(Protocol):
    """REST market data (fallback / resync for the streaming sources)."""

    async def fetch_book(self, token_id: str) -> RawMessage: ...

    async def fetch_clob_market(self, condition_id: str) -> RawMessage: ...

    async def fetch_server_time_ms(self) -> int: ...


class StreamingSource(Protocol):
    """A WebSocket-like source emitting raw frames and connection events."""

    @property
    def connected(self) -> bool: ...

    def stream(self) -> AsyncIterator[RawMessage]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CancelResult:
    cancelled: tuple[str, ...]
    not_cancelled: dict[str, str]
    ok: bool


class TradingProvider(Protocol):
    """Order entry. Implementations: PolymarketLiveTrading, PaperExchange, FakeExchange."""

    name: str

    async def submit(self, intent: OrderIntent, market: MarketDefinition) -> OrderAck: ...

    async def cancel(self, exchange_order_id: str) -> CancelResult: ...

    async def cancel_all(self) -> CancelResult: ...

    async def poll_events(self) -> list[ExecutionEvent]: ...

    async def find_orders_since(self, token_id: str, since_ms: int) -> list[ExecutionEvent]:
        """Used to resolve UNKNOWN submissions: fills/updates for a token since a time."""
        ...


class AccountProvider(Protocol):
    async def account_snapshot(self) -> AccountSnapshot: ...


class ResolutionProvider(Protocol):
    async def resolved_outcome(self, market: MarketDefinition) -> str | None:
        """Winning outcome label once *officially* resolved, else None."""
        ...


class ComplianceProvider(Protocol):
    async def is_trading_permitted(self) -> tuple[bool, str]: ...


class BalanceView(Protocol):
    def available_collateral_usd(self) -> Decimal: ...
