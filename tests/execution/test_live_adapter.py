"""Live adapter mapping, tested against a FakeSecureClient built from the SDK's own
response models (polymarket-client 0.10.0). This checks our translation and the
SDK-method whitelist; it does NOT verify behaviour against the real exchange."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from polymarket._internal.actions.orders.post import parse_order_response
from polymarket.errors import PolymarketError
from polymarket.models.clob.account import BalanceAllowance, ClobTrade, OpenOrder
from polymarket.models.clob.cancel import CancelOrdersResponse

import polymarket_bot.adapters.polymarket_live as live
from polymarket_bot.adapters.polymarket_live import LiveVenueError, PolymarketLiveVenue
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.orders import Fill, OrderIntent, OrderUpdate
from polymarket_bot.domain.types import OrderPurpose, OrderStatus, OrderType, Side
from tests.factories import COND, CRYPTO_FEES, T0, UP, make_market

D = Decimal
WALLET = "0x" + "1" * 40
ALLOWED = {
    "create_market_order",
    "post_order",
    "cancel_order",
    "cancel_all",
    "get_order",
    "list_open_orders",
    "list_account_trades",
    "get_balance_allowance",
    "list_positions",
    "get_closed_only_mode",
    "close",
}


class _Pager:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    async def iter_items(self) -> AsyncIterator[Any]:
        for item in self._items:
            yield item


def _trade(tid: str, order_id: str, price: str, size: str, status: str = "MATCHED") -> ClobTrade:
    return ClobTrade.model_validate(
        {
            "id": tid,
            "market": COND,
            "asset_id": UP,
            "owner": "o",
            "maker_address": WALLET,
            "taker_order_id": order_id,
            "side": "BUY",
            "trader_side": "TAKER",
            "price": price,
            "size": size,
            "outcome": "Up",
            "status": f"TRADE_STATUS_{status}",
            "fee_rate_bps": "0",
            "bucket_index": 0,
            "transaction_hash": "0x" + "a" * 64,
            "maker_orders": [],
            "match_time": str((T0 + 1000) // 1000),
            "last_update": str((T0 + 1000) // 1000),
        }
    )


class FakeSecureClient:
    """Records every SDK call; anything outside the whitelist fails the test."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.post_payload: dict[str, Any] = {}
        self.trades: list[ClobTrade] = []
        self.order_status: dict[str, Any] = {}
        self.signed: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"adapter called non-whitelisted SDK method {name!r}")

    def _log(self, name: str) -> None:
        assert name in ALLOWED, name
        self.calls.append(name)

    async def create_market_order(self, **kw: Any) -> dict[str, Any]:
        self._log("create_market_order")
        self.signed = kw
        return {"signed": kw}

    async def post_order(self, signed: Any) -> Any:
        self._log("post_order")
        return parse_order_response(self.post_payload)

    async def cancel_order(self, *, order_id: str) -> CancelOrdersResponse:
        self._log("cancel_order")
        return CancelOrdersResponse.model_validate({"canceled": [order_id], "not_canceled": {}})

    async def cancel_all(self) -> CancelOrdersResponse:
        self._log("cancel_all")
        return CancelOrdersResponse.model_validate({"canceled": [], "not_canceled": {"x": "gone"}})

    def list_account_trades(self, **kw: Any) -> _Pager:
        self._log("list_account_trades")
        return _Pager(self.trades)

    async def get_order(self, *, order_id: str) -> Any:
        self._log("get_order")
        if order_id not in self.order_status:
            raise PolymarketError("not found")
        return self.order_status[order_id]

    async def get_balance_allowance(self, **kw: Any) -> BalanceAllowance:
        self._log("get_balance_allowance")
        units = 150_000_000 if kw["asset_type"] == "COLLATERAL" else 12_500_000
        return BalanceAllowance.model_validate({"balance": str(units), "allowances": {}})

    def list_positions(self, **kw: Any) -> _Pager:
        self._log("list_positions")
        return _Pager([])

    def list_open_orders(self, **kw: Any) -> _Pager:
        self._log("list_open_orders")
        return _Pager([])

    async def close(self) -> None:
        self._log("close")


def _intent(side: Side = Side.BUY, order_type: OrderType = OrderType.FAK) -> OrderIntent:
    buy = side is Side.BUY
    return OrderIntent(
        intent_id="oi-1",
        decision_id="rd-1",
        condition_id=COND,
        market_slug="btc-updown-5m-1790127600",
        token_id=UP,
        outcome="Up",
        side=side,
        order_type=order_type,
        limit_price=D("0.62") if buy else D("0.55"),
        buy_amount_usd=D("10") if buy else None,
        sell_shares=None if buy else D("16"),
        purpose=OrderPurpose.ENTRY if buy else OrderPurpose.EXIT,
        created_ms=T0,
    )


def _venue() -> tuple[PolymarketLiveVenue, FakeSecureClient]:
    fake = FakeSecureClient()
    return PolymarketLiveVenue(fake, SimulatedClock(T0 + 1000), WALLET), fake  # type: ignore[arg-type]


def _accepted(status: str, making: str, taking: str) -> dict[str, Any]:
    return {
        "errorMsg": "",
        "makingAmount": making,
        "orderID": "0xord1",
        "status": status,
        "success": True,
        "takingAmount": taking,
        "tradeIDs": ["t1"],
        "transactionsHashes": [],
    }


async def test_buy_uses_sign_then_post_with_limit_and_fak() -> None:
    venue, fake = _venue()
    fake.post_payload = _accepted("matched", "9.92", "16")
    ack = await venue.submit(_intent(), make_market())
    assert ack.accepted and ack.exchange_order_id == "0xord1"
    assert fake.calls == ["create_market_order", "post_order"]
    assert fake.signed == {
        "token_id": UP,
        "side": "BUY",
        "amount": "10",
        "max_price": "0.62",
        "order_type": "FAK",
    }


async def test_sell_passes_shares_and_min_price() -> None:
    venue, fake = _venue()
    fake.post_payload = _accepted("matched", "16", "8.8")
    await venue.submit(_intent(Side.SELL), make_market())
    assert fake.signed["side"] == "SELL"
    assert fake.signed["shares"] == "16" and fake.signed["min_price"] == "0.55"


async def test_resting_order_types_are_refused() -> None:
    venue, fake = _venue()
    with pytest.raises(LiveVenueError):
        await venue.submit(_intent(order_type=OrderType.GTC), make_market())
    assert fake.calls == []


async def test_rejection_is_mapped() -> None:
    venue, fake = _venue()
    fake.post_payload = {
        "errorMsg": "not enough balance / allowance",
        "makingAmount": "",
        "orderID": "",
        "status": "",
        "success": False,
        "takingAmount": "",
    }
    ack = await venue.submit(_intent(), make_market())
    assert not ack.accepted and ack.status is OrderStatus.REJECTED


async def test_fills_from_trades_then_terminal_update() -> None:
    venue, fake = _venue()
    fake.post_payload = _accepted("matched", "9.92", "16")
    await venue.submit(_intent(), make_market())
    fake.trades = [
        _trade("t1", "0xord1", "0.62", "10"),
        _trade("t2", "0xord1", "0.62", "6", status="RETRYING"),  # not counted yet
        _trade("t9", "0xother", "0.50", "3"),  # someone else's order
    ]
    events = await venue.poll_events()
    fills = [e for e in events if isinstance(e, Fill)]
    assert [(f.fill_id, f.shares) for f in fills] == [("live-t1", D("10"))]
    assert fills[0].fee_usd == CRYPTO_FEES.taker_fee(D("10"), D("0.62"))
    assert not any(isinstance(e, OrderUpdate) for e in events)
    fake.trades[1] = _trade("t2", "0xord1", "0.62", "6", status="CONFIRMED")
    events = await venue.poll_events()
    upd = [e for e in events if isinstance(e, OrderUpdate)]
    assert len(upd) == 1 and upd[0].cumulative_filled_shares == D("16")
    assert await venue.poll_events() == []  # terminal orders are not polled again


async def test_delayed_order_waits_for_venue_status() -> None:
    venue, fake = _venue()
    fake.post_payload = _accepted("delayed", "0", "0")
    await venue.submit(_intent(), make_market())
    assert await venue.poll_events() == []  # get_order not found yet -> nothing assumed
    fake.order_status["0xord1"] = OpenOrder.model_validate(
        {
            "id": "0xord1",
            "market": COND,
            "asset_id": UP,
            "owner": "o",
            "maker_address": WALLET,
            "side": "BUY",
            "price": "0.62",
            "original_size": "16",
            "size_matched": "0",
            "outcome": "Up",
            "order_type": "FAK",
            "status": "CANCELED",
            "created_at": str(T0 // 1000),
        }
    )
    events = await venue.poll_events()
    assert [type(e) for e in events] == [OrderUpdate]
    assert events[0].status is OrderStatus.CANCELLED  # type: ignore[union-attr]


async def test_account_snapshot_units_and_cancel_mapping() -> None:
    venue, fake = _venue()
    fake.post_payload = _accepted("matched", "9.92", "16")
    await venue.submit(_intent(), make_market())
    snap = await venue.account_snapshot()
    assert snap.complete and snap.collateral_usd == D("150")
    assert snap.positions == {UP: D("12.5")}
    assert (await venue.cancel("0xord1")).ok
    assert not (await venue.cancel_all()).ok
    assert set(fake.calls) <= ALLOWED


async def test_connect_never_uses_the_wallet_deploying_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSecureClient()

    async def forbidden(*a: Any, **k: Any) -> Any:
        raise AssertionError("public create() may deploy a wallet; must not be used")

    async def private_create(**kw: Any) -> Any:
        assert kw["wallet"] == WALLET
        return fake

    async def closed_only() -> bool:
        return False

    fake.get_closed_only_mode = closed_only  # type: ignore[method-assign]
    monkeypatch.setattr(live.AsyncSecureClient, "create", forbidden)
    monkeypatch.setattr(live.AsyncSecureClient, "_create", private_create)
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "ab" * 32)
    monkeypatch.setenv("POLYMARKET_WALLET_ADDRESS", WALLET)
    venue = await PolymarketLiveVenue.connect(SimulatedClock(T0))
    assert venue.wallet == WALLET


def test_sdk_private_constructor_still_exists_in_pinned_version() -> None:
    """The adapter relies on AsyncSecureClient._create (0.10.0); fail loudly on upgrade."""
    import inspect  # noqa: PLC0415

    from polymarket import AsyncSecureClient  # noqa: PLC0415
    from polymarket.version import __version__  # noqa: PLC0415

    assert __version__ == "0.10.0"
    params = inspect.signature(AsyncSecureClient._create).parameters
    assert {"private_key", "wallet", "credentials", "validate_credentials"} <= set(params)
