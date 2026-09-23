"""Live Polymarket venue over the official SDK (``polymarket-client==0.10.0``).

STATUS: IMPLEMENTED against the SDK source, **NOT VERIFIED end-to-end** — it has
never placed an order on the real exchange (see docs/progress.md). It is only
reachable after every live-lock precondition passes (``promotion/live_lock.py``).

The only module that imports ``polymarket`` and ``load_polymarket_credentials``
(enforced by tests/security). Whitelisted SDK operations — nothing else of the
SDK object is reachable from the rest of the bot:

* ``AsyncSecureClient._create``: builds the authenticated client *without*
  ``_ensure_wallet_ready``, which can deploy a Deposit Wallet on-chain
  (docs/research.md §1.1, corrected) — this adapter must never do that;
* ``create_market_order`` (sign only) + ``post_order`` (no allowance-recovery
  re-post: ``place_*`` methods may send ``approve(max)`` on-chain);
* ``cancel_order`` / ``cancel_all``;
* reads: ``get_order``, ``list_open_orders``, ``list_account_trades``,
  ``get_balance_allowance``, ``list_positions``, ``get_closed_only_mode``.

Fill fees are recomputed with the market's official fee schedule (rounded up),
the same formula the risk and paper engines use; reconciliation compares the
resulting cash with the venue's collateral balance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from polymarket import AsyncSecureClient
from polymarket.errors import PolymarketError
from polymarket.models.clob.api_key import ApiKeyCreds

from polymarket_bot.domain.clock import Clock
from polymarket_bot.domain.market import FeeSchedule, MarketDefinition
from polymarket_bot.domain.orders import (
    AccountSnapshot,
    ExecutionEvent,
    Fill,
    OpenOrderView,
    OrderAck,
    OrderIntent,
    OrderUpdate,
)
from polymarket_bot.domain.types import OrderStatus, OrderType, Side
from polymarket_bot.ports import CancelResult
from polymarket_bot.security.secrets import load_polymarket_credentials

log = logging.getLogger(__name__)

ZERO = Decimal(0)
BASE_UNITS = Decimal(10) ** 6  # pUSD and CTF shares use 6 decimals (SDK orders/math.py)
SHARE_TOLERANCE = Decimal("0.000001")
COUNTED_TRADE_STATUSES = frozenset({"MATCHED", "MATCHED_NOT_BROADCASTED", "MINED", "CONFIRMED"})
OPEN_ORDER_STATUSES = frozenset({"LIVE", "DELAYED", "ORDER_STATUS_LIVE", "ORDER_STATUS_DELAYED"})


class LiveVenueError(RuntimeError):
    pass


@dataclass
class _Tracked:
    intent: OrderIntent
    order_id: str
    fees: FeeSchedule
    submitted_ms: int
    expected_shares: Decimal | None  # matched at placement (None when delayed)
    seen_trades: set[str] = field(default_factory=set)
    cumulative: Decimal = ZERO
    terminal: bool = False


def _ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=dt.tzinfo or UTC).timestamp() * 1000)


class PolymarketLiveVenue:
    """TradingProvider + AccountProvider for the real exchange."""

    name = "polymarket_live"

    def __init__(self, client: AsyncSecureClient, clock: Clock, wallet: str) -> None:
        self._client = client
        self._clock = clock
        self.wallet = wallet
        self._orders: dict[str, _Tracked] = {}

    @classmethod
    async def connect(cls, clock: Clock) -> PolymarketLiveVenue:
        creds = load_polymarket_credentials()
        api: ApiKeyCreds | None = None
        if creds.api_key and creds.api_secret and creds.api_passphrase:
            api = ApiKeyCreds.model_validate(
                {
                    "apiKey": creds.api_key.reveal(),
                    "secret": creds.api_secret.reveal(),
                    "passphrase": creds.api_passphrase.reveal(),
                }
            )
        # Private on purpose: public create() may deploy a wallet (module docstring).
        client = await AsyncSecureClient._create(
            private_key=creds.private_key.reveal(),
            wallet=creds.wallet_address,
            credentials=api,
            validate_credentials=True,
        )
        try:
            if await client.get_closed_only_mode():
                raise LiveVenueError("account is in closed-only mode; refusing to trade")
        except BaseException:
            await client.close()
            raise
        return cls(client, clock, creds.wallet_address)

    async def close(self) -> None:
        await self._client.close()

    # ------------------------------------------------------------------ TradingProvider
    async def submit(self, intent: OrderIntent, market: MarketDefinition) -> OrderAck:
        if intent.order_type not in (OrderType.FAK, OrderType.FOK):
            raise LiveVenueError(f"order type {intent.order_type} forbidden by live policy")
        order_type: Literal["FAK", "FOK"] = "FOK" if intent.order_type is OrderType.FOK else "FAK"
        if intent.side is Side.BUY:
            assert intent.buy_amount_usd is not None
            signed = await self._client.create_market_order(
                token_id=intent.token_id,
                side="BUY",
                amount=str(intent.buy_amount_usd),
                max_price=str(intent.limit_price),
                order_type=order_type,
            )
        else:
            assert intent.sell_shares is not None
            signed = await self._client.create_market_order(
                token_id=intent.token_id,
                side="SELL",
                shares=str(intent.sell_shares),
                min_price=str(intent.limit_price),
                order_type=order_type,
            )
        # Exceptions from here propagate: the execution engine marks the order UNKNOWN.
        response = await self._client.post_order(signed)
        now = self._clock.now_ms()
        if not response.ok:
            return OrderAck(
                intent.intent_id,
                False,
                OrderStatus.REJECTED,
                None,
                ZERO,
                None,
                ZERO,
                now,
                error_code=response.code,
                error_message=response.message,
            )
        # BUY: taking = shares received; SELL: making = shares given.
        matched = response.taking_amount if intent.side is Side.BUY else response.making_amount
        expected = matched if response.status == "matched" else None
        self._orders[response.order_id] = _Tracked(
            intent, response.order_id, market.fee_schedule, now, expected
        )
        return OrderAck(
            intent.intent_id, True, OrderStatus.LIVE, response.order_id, ZERO, None, ZERO, now
        )

    async def cancel(self, exchange_order_id: str) -> CancelResult:
        return _cancel_result(await self._client.cancel_order(order_id=exchange_order_id))

    async def cancel_all(self) -> CancelResult:
        return _cancel_result(await self._client.cancel_all())

    async def poll_events(self) -> list[ExecutionEvent]:
        events: list[ExecutionEvent] = []
        for tracked in [t for t in self._orders.values() if not t.terminal]:
            events.extend(await self._poll_order(tracked))
        return events

    async def _poll_order(self, t: _Tracked) -> list[ExecutionEvent]:
        out: list[ExecutionEvent] = []
        # ``after`` = unix seconds (legacy CLOB /data/trades semantics; NOT VERIFIED on V2).
        since = str(t.submitted_ms // 1000 - 60)
        async for trade in self._client.list_account_trades(
            asset_id=t.intent.token_id, after=since
        ).iter_items():
            if trade.taker_order_id != t.order_id or trade.id in t.seen_trades:
                continue
            if str(trade.status).upper() not in COUNTED_TRADE_STATUSES:
                continue  # RETRYING/FAILED are not fills (reconciliation catches reversals)
            t.seen_trades.add(trade.id)
            t.cumulative += trade.size
            out.append(
                Fill(
                    fill_id=f"live-{trade.id}",
                    intent_id=t.intent.intent_id,
                    exchange_order_id=t.order_id,
                    condition_id=t.intent.condition_id,
                    token_id=t.intent.token_id,
                    outcome=t.intent.outcome,
                    side=t.intent.side,
                    price=trade.price,
                    shares=trade.size,
                    fee_usd=t.fees.taker_fee(trade.size, trade.price),
                    ts_ms=_ms(trade.matched_at),
                    liquidity="taker",
                    source="live",
                )
            )
        if t.expected_shares is not None and t.cumulative + SHARE_TOLERANCE >= t.expected_shares:
            out.append(self._terminal(t, "matched at placement"))
        elif t.expected_shares is None:
            out.extend(await self._check_delayed(t))
        return out

    async def _check_delayed(self, t: _Tracked) -> list[ExecutionEvent]:
        try:
            order = await self._client.get_order(order_id=t.order_id)
        except PolymarketError:
            return []  # still unknown: the engine's UNKNOWN/timeout logic applies
        if str(order.status).upper() in OPEN_ORDER_STATUSES:
            return []
        t.expected_shares = order.size_matched
        if t.cumulative + SHARE_TOLERANCE >= order.size_matched:
            return [self._terminal(t, f"order {order.status}")]
        return []

    def _terminal(self, t: _Tracked, detail: str) -> OrderUpdate:
        t.terminal = True
        intent = t.intent
        full = (
            intent.sell_shares is not None and t.cumulative + SHARE_TOLERANCE >= intent.sell_shares
        )
        status = OrderStatus.FILLED if full else OrderStatus.CANCELLED  # FAK remainder killed
        return OrderUpdate(
            t.order_id, intent.intent_id, status, t.cumulative, self._clock.now_ms(), detail
        )

    async def find_orders_since(self, token_id: str, since_ms: int) -> list[ExecutionEvent]:
        """Venue view for UNKNOWN resolution. Our intent ids are not known to the venue,
        so an order that timed out before its ack cannot be attributed automatically:
        the engine then halts for the operator (fail closed)."""
        return [
            OrderUpdate(order.id, None, OrderStatus.LIVE, order.size_matched, _ms(order.created_at))
            async for order in self._client.list_open_orders(asset_id=token_id).iter_items()
            if _ms(order.created_at) >= since_ms
        ]

    # ------------------------------------------------------------------ AccountProvider
    async def account_snapshot(self) -> AccountSnapshot:
        now = self._clock.now_ms()
        complete = True
        collateral = ZERO
        positions: dict[str, Decimal] = {}
        open_orders: list[OpenOrderView] = []
        try:
            bal = await self._client.get_balance_allowance(asset_type="COLLATERAL")
            collateral = Decimal(bal.balance) / BASE_UNITS
            tokens: set[str] = set()
            async for pos in self._client.list_positions().iter_items():
                if pos.current_size > 0:
                    tokens.add(str(pos.asset_id))
            tokens.update(t.intent.token_id for t in self._orders.values())
            for token in sorted(tokens):
                cond = await self._client.get_balance_allowance(
                    asset_type="CONDITIONAL", token_id=token
                )
                shares = Decimal(cond.balance) / BASE_UNITS
                if shares > 0:
                    positions[token] = shares
            open_orders = [
                OpenOrderView(
                    exchange_order_id=order.id,
                    token_id=str(order.asset_id),
                    side=Side(str(order.side).upper()),
                    price=order.price,
                    original_size=order.original_size,
                    size_matched=order.size_matched,
                    status=str(order.status),
                )
                async for order in self._client.list_open_orders().iter_items()
            ]
        except (PolymarketError, ValueError) as exc:
            log.warning("live account snapshot incomplete: %s", type(exc).__name__)
            complete = False
        return AccountSnapshot(
            collateral_usd=collateral,
            positions=positions,
            open_orders=tuple(open_orders),
            fetched_ms=now,
            source=self.name,
            complete=complete,
        )

    def describe(self) -> dict[str, Any]:
        return {"venue": self.name, "wallet": self.wallet, "tracked_orders": len(self._orders)}


def _cancel_result(resp: Any) -> CancelResult:
    not_cancelled = {str(k): str(v) for k, v in resp.not_canceled.items()}
    return CancelResult(tuple(str(i) for i in resp.canceled), not_cancelled, not not_cancelled)
