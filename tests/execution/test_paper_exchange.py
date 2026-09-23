"""Paper exchange realism: latency, book-at-arrival matching, partial fills, fees."""

from __future__ import annotations

from decimal import Decimal

import pytest

from polymarket_bot.adapters.paper_exchange import PaperExchange
from polymarket_bot.config.app_config import PaperExchangeConfig
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.market import OrderBookSnapshot, OutcomeToken
from polymarket_bot.domain.orders import Fill, OrderIntent, OrderUpdate
from polymarket_bot.domain.types import OrderPurpose, OrderStatus, OrderType, Side
from tests.factories import COND, CRYPTO_FEES, DOWN, T0, UP, make_book, make_market

D = Decimal
NOW = T0 + 60_000
LATENCY = 150 + 50  # latency_ms + taker_delay_ms with zero jitter


def _cfg(**kw: object) -> PaperExchangeConfig:
    base: dict[str, object] = {
        "initial_balance_usd": D("100"),
        "latency_ms": 150,
        "latency_jitter_ms": 0,
        "taker_delay_ms": 50,
        "liquidity_replenish_ms": 2000,
    }
    base.update(kw)
    return PaperExchangeConfig.model_validate(base)


class Books:
    """Mutable book source: tests change the book between submit and arrival."""

    def __init__(self) -> None:
        self.books: dict[str, OrderBookSnapshot | None] = {}

    def __call__(self, token: str) -> OrderBookSnapshot | None:
        return self.books.get(token)


def _setup(**kw: object) -> tuple[PaperExchange, SimulatedClock, Books]:
    clock = SimulatedClock(NOW)
    books = Books()
    books.books[UP] = make_book(
        UP, [("0.60", "50"), ("0.59", "100")], [("0.62", "10"), ("0.63", "20")], NOW
    )
    return PaperExchange(_cfg(**kw), clock, books), clock, books


def _buy(amount: str, limit: str, order_type: OrderType = OrderType.FAK, n: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=f"oi-{n}",
        decision_id=f"d-{n}",
        condition_id=COND,
        market_slug="btc-updown-5m-1790127600",
        token_id=UP,
        outcome="Up",
        side=Side.BUY,
        order_type=order_type,
        limit_price=D(limit),
        buy_amount_usd=D(amount),
        sell_shares=None,
        purpose=OrderPurpose.ENTRY,
        created_ms=NOW,
    )


def _sell(
    shares: str, limit: str, order_type: OrderType = OrderType.FAK, n: int = 9
) -> OrderIntent:
    return OrderIntent(
        intent_id=f"oi-{n}",
        decision_id=f"d-{n}",
        condition_id=COND,
        market_slug="btc-updown-5m-1790127600",
        token_id=UP,
        outcome="Up",
        side=Side.SELL,
        order_type=order_type,
        limit_price=D(limit),
        buy_amount_usd=None,
        sell_shares=D(shares),
        purpose=OrderPurpose.EXIT,
        created_ms=NOW,
    )


def _fills(events: list[Fill | OrderUpdate]) -> list[Fill]:
    return [e for e in events if isinstance(e, Fill)]


def _updates(events: list[Fill | OrderUpdate]) -> list[OrderUpdate]:
    return [e for e in events if isinstance(e, OrderUpdate)]


async def test_order_is_not_matched_before_latency_elapses() -> None:
    ex, clock, _ = _setup()
    ack = await ex.submit(_buy("5", "0.62"), make_market())
    assert ack.accepted and ack.status is OrderStatus.LIVE
    clock.advance_to(NOW + LATENCY - 1)
    assert await ex.poll_events() == []
    assert ex.pending_count() == 1


async def test_matching_uses_book_observed_at_arrival_not_at_submission() -> None:
    ex, clock, books = _setup()
    await ex.submit(_buy("5", "0.62"), make_market())
    # The ask moves away while the order is in flight: the order must miss.
    books.books[UP] = make_book(UP, [("0.63", "50")], [("0.65", "100")], NOW + 100)
    clock.advance_to(NOW + LATENCY)
    events = await ex.poll_events()
    assert _fills(events) == []
    (upd,) = _updates(events)
    assert upd.status is OrderStatus.CANCELLED
    assert upd.cumulative_filled_shares == 0
    assert ex.cash_usd == D("100")


async def test_buy_fak_walks_levels_within_limit_and_cancels_remainder() -> None:
    ex, clock, _ = _setup()
    await ex.submit(_buy("50", "0.63"), make_market())
    clock.advance_to(NOW + LATENCY)
    events = await ex.poll_events()
    fills = _fills(events)
    assert [(f.price, f.shares) for f in fills] == [(D("0.62"), D("10")), (D("0.63"), D("20"))]
    # Fees are the official taker fee per level, rounded up; never charged at the mid.
    assert [f.fee_usd for f in fills] == [
        CRYPTO_FEES.taker_fee(D("10"), D("0.62")),
        CRYPTO_FEES.taker_fee(D("20"), D("0.63")),
    ]
    (upd,) = _updates(events)
    assert upd.status is OrderStatus.CANCELLED  # FAK remainder cancelled
    assert upd.cumulative_filled_shares == D("30")
    spent = D("0.62") * 10 + D("0.63") * 20 + sum(f.fee_usd for f in fills)
    assert ex.cash_usd == D("100") - spent
    assert ex.positions[UP] == D("30")


async def test_buy_limit_stops_the_walk() -> None:
    ex, clock, _ = _setup()
    await ex.submit(_buy("50", "0.62"), make_market())
    clock.advance_to(NOW + LATENCY)
    fills = _fills(await ex.poll_events())
    assert [(f.price, f.shares) for f in fills] == [(D("0.62"), D("10"))]


async def test_buy_within_budget_is_complete() -> None:
    ex, clock, _ = _setup()
    await ex.submit(_buy("3.1", "0.62"), make_market())
    clock.advance_to(NOW + LATENCY)
    events = await ex.poll_events()
    (fill,) = _fills(events)
    assert fill.shares == D("5")
    assert _updates(events)[0].status is OrderStatus.FILLED


async def test_fok_is_all_or_nothing() -> None:
    ex, clock, _ = _setup()
    await ex.submit(_buy("50", "0.63", OrderType.FOK), make_market())
    clock.advance_to(NOW + LATENCY)
    events = await ex.poll_events()
    assert _fills(events) == []
    assert _updates(events)[0].status is OrderStatus.CANCELLED
    await ex.submit(_buy("6.2", "0.62", OrderType.FOK, n=2), make_market())
    clock.advance_to(NOW + 2 * LATENCY)
    events = await ex.poll_events()
    assert sum(f.shares for f in _fills(events)) == D("10")
    assert _updates(events)[0].status is OrderStatus.FILLED


async def test_own_fills_consume_liquidity_until_replenished() -> None:
    ex, clock, books = _setup()
    await ex.submit(_buy("50", "0.62"), make_market())
    clock.advance_to(NOW + LATENCY)
    assert sum(f.shares for f in _fills(await ex.poll_events())) == D("10")
    # An unrelated book update re-displays the same size: it must stay hidden.
    books.books[UP] = make_book(UP, [("0.60", "50")], [("0.62", "10")], NOW + 300)
    await ex.submit(_buy("50", "0.62", n=2), make_market())
    clock.advance_to(NOW + 2 * LATENCY)
    assert _fills(await ex.poll_events()) == []
    # After the replenish window the displayed size is available again.
    clock.advance_to(NOW + LATENCY + 2001)
    await ex.submit(_buy("50", "0.62", n=3), make_market())
    clock.advance_to(NOW + 2 * LATENCY + 2001)
    assert sum(f.shares for f in _fills(await ex.poll_events())) == D("10")


async def test_sell_walks_bids_down_to_limit() -> None:
    ex, clock, _ = _setup()
    ex.positions[UP] = D("80")
    await ex.submit(_sell("80", "0.59"), make_market())
    clock.advance_to(NOW + LATENCY)
    events = await ex.poll_events()
    fills = _fills(events)
    assert [(f.price, f.shares) for f in fills] == [(D("0.60"), D("50")), (D("0.59"), D("30"))]
    assert _updates(events)[0].status is OrderStatus.FILLED
    assert ex.positions[UP] == 0
    fees = sum(f.fee_usd for f in fills)
    assert ex.cash_usd == D("100") + D("0.60") * 50 + D("0.59") * 30 - fees


@pytest.mark.parametrize(
    ("intent_factory", "reason"),
    [
        (lambda: _buy("5", "0.62", OrderType.GTC), "not supported"),
        (lambda: _buy("5", "0.625"), "not on tick"),
        (lambda: _buy("99", "0.62"), "balance"),  # 99 + worst-case fee > 100
        (lambda: _sell("1", "0.60"), "not enough shares"),
    ],
)
async def test_invalid_orders_are_rejected(intent_factory: object, reason: str) -> None:
    ex, _, _ = _setup()
    ack = await ex.submit(intent_factory(), make_market())  # type: ignore[operator]
    assert not ack.accepted
    assert ack.status is OrderStatus.REJECTED
    assert reason in (ack.error_message or "")
    assert ex.pending_count() == 0


async def test_token_outside_market_is_rejected() -> None:
    ex, _, _ = _setup()
    other = make_market(tokens=(OutcomeToken("111", "Up"), OutcomeToken("222", "Down")))
    ack = await ex.submit(_buy("5", "0.62"), other)
    assert not ack.accepted


async def test_missing_book_cancels_instead_of_guessing() -> None:
    ex, clock, books = _setup()
    await ex.submit(_buy("5", "0.62"), make_market())
    books.books[UP] = None
    clock.advance_to(NOW + LATENCY)
    events = await ex.poll_events()
    assert _fills(events) == []
    assert _updates(events)[0].detail == "no valid book"


async def test_cancel_before_arrival() -> None:
    ex, clock, _ = _setup()
    ack = await ex.submit(_buy("5", "0.62"), make_market())
    assert ack.exchange_order_id is not None
    res = await ex.cancel(ack.exchange_order_id)
    assert res.cancelled == (ack.exchange_order_id,)
    clock.advance_to(NOW + LATENCY)
    events = await ex.poll_events()
    assert _fills(events) == []
    assert _updates(events)[0].status is OrderStatus.CANCELLED


async def test_history_supports_unknown_order_resolution() -> None:
    ex, clock, _ = _setup()
    await ex.submit(_buy("5", "0.62"), make_market())
    clock.advance_to(NOW + LATENCY)
    await ex.poll_events()
    history = await ex.find_orders_since(UP, NOW)
    assert any(isinstance(e, Fill) and e.intent_id == "oi-1" for e in history)
    assert await ex.find_orders_since(DOWN, NOW) == []
    assert await ex.find_orders_since(UP, NOW + 10 * LATENCY) == []


async def test_settlement_pays_one_per_winning_share() -> None:
    ex, _, _ = _setup()
    ex.positions[UP] = D("10")
    ex.positions[DOWN] = D("4")
    assert ex.settle(UP, DOWN) == D("10")
    assert ex.cash_usd == D("110")
    assert ex.positions == {}
    snap = await ex.account_snapshot()
    assert snap.positions == {}
    assert snap.collateral_usd == D("110")


async def test_jitter_is_seeded_and_bounded() -> None:
    delays = []
    for _ in range(2):
        clock = SimulatedClock(NOW)
        books = Books()
        books.books[UP] = make_book(UP, [("0.60", "50")], [("0.62", "1000")], NOW)
        ex = PaperExchange(_cfg(latency_jitter_ms=100, seed=11), clock, books)
        run = []
        for n in range(5):
            await ex.submit(_buy("1", "0.62", n=n), make_market())
        for t in range(NOW, NOW + 400):
            clock.advance_to(t)
            events = await ex.poll_events()
            run.extend(t - NOW for e in events if isinstance(e, OrderUpdate))
        delays.append(run)
    assert delays[0] == delays[1]
    assert all(LATENCY <= d <= LATENCY + 100 for d in delays[0])
