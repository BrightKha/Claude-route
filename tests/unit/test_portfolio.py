"""Position accounting: fees in cost basis, conservative marking, settlement, invariants."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from polymarket_bot.domain.orders import Fill
from polymarket_bot.domain.types import OrderPurpose, Side
from polymarket_bot.portfolio.portfolio import InvariantError, Portfolio
from polymarket_bot.risk.rates import RateTracker
from tests.factories import COND, CRYPTO_FEES, DOWN, T0, UP

D = Decimal
SLUG = "btc-updown-5m-1790127600"
END = T0 + 300_000


def _fill(side: Side, price: str, shares: str, n: int = 0, token: str = UP, ts: int = T0) -> Fill:
    p, s = D(price), D(shares)
    return Fill(
        fill_id=f"f-{n}",
        intent_id=f"oi-{n}",
        exchange_order_id=f"x-{n}",
        condition_id=COND,
        token_id=token,
        outcome="Up" if token == UP else "Down",
        side=side,
        price=p,
        shares=s,
        fee_usd=CRYPTO_FEES.taker_fee(s, p),
        ts_ms=ts,
        liquidity="taker",
        source="test",
    )


def _apply(p: Portfolio, f: Fill) -> object:
    return p.apply_fill(f, market_slug=SLUG, window_end_ms=END)


def test_buy_puts_fees_in_cost_basis() -> None:
    p = Portfolio.with_cash(D("100"), T0)
    f = _fill(Side.BUY, "0.60", "10")
    _apply(p, f)
    cost = D("6") + f.fee_usd
    assert p.cash_usd == D("100") - cost
    pos = p.positions[UP]
    assert pos.shares == D("10")
    assert pos.cost_basis_usd == cost
    assert p.fees_paid_usd == f.fee_usd
    assert p.exposure_usd == cost


def test_partial_then_full_sell_realizes_pnl_net_of_fees() -> None:
    p = Portfolio.with_cash(D("100"), T0)
    buy = _fill(Side.BUY, "0.60", "10")
    _apply(p, buy)
    s1 = _fill(Side.SELL, "0.70", "4", n=1)
    assert _apply(p, s1) is None
    basis_4 = (D("6") + buy.fee_usd) * 4 / 10
    assert p.realized_pnl_usd == D("2.8") - s1.fee_usd - basis_4
    s2 = _fill(Side.SELL, "0.50", "6", n=2)
    trade = _apply(p, s2)
    assert trade is not None
    assert UP not in p.positions
    total_fees = buy.fee_usd + s1.fee_usd + s2.fee_usd
    expected = D("2.8") + D("3.0") - D("6") - total_fees
    assert trade.pnl_usd == expected  # type: ignore[attr-defined]
    assert trade.fees_usd == total_fees  # type: ignore[attr-defined]
    assert p.cash_usd == D("100") + expected


def test_selling_more_than_held_is_an_invariant_violation() -> None:
    p = Portfolio.with_cash(D("100"), T0)
    _apply(p, _fill(Side.BUY, "0.60", "10"))
    with pytest.raises(InvariantError):
        _apply(p, _fill(Side.SELL, "0.60", "11", n=1))
    with pytest.raises(InvariantError):
        _apply(p, _fill(Side.SELL, "0.60", "1", n=2, token=DOWN))


def test_buying_beyond_cash_is_an_invariant_violation() -> None:
    p = Portfolio.with_cash(D("5"), T0)
    with pytest.raises(InvariantError):
        _apply(p, _fill(Side.BUY, "0.60", "10"))


def test_settlement_pays_winner_and_zeroes_loser() -> None:
    p = Portfolio.with_cash(D("100"), T0)
    up = _fill(Side.BUY, "0.60", "10")
    down = _fill(Side.BUY, "0.30", "10", n=1, token=DOWN)
    _apply(p, up)
    _apply(p, down)
    trades = p.settle(COND, "Up", END + 5_000)
    by_token = {t.token_id: t for t in trades}
    assert by_token[UP].pnl_usd == D("10") - (D("6") + up.fee_usd)
    assert by_token[DOWN].pnl_usd == -(D("3") + down.fee_usd)
    assert p.positions == {}
    assert p.consecutive_losses in (0, 1)
    assert p.cash_usd == D("100") - D("9") - up.fee_usd - down.fee_usd + D("10")


def test_loss_streak_and_reset() -> None:
    p = Portfolio.with_cash(D("100"), T0)
    for n, winner in enumerate(["Down", "Down", "Up"]):
        f = _fill(Side.BUY, "0.50", "2", n=n, ts=T0 + n)
        _apply(p, f)
        p.settle(COND, winner, T0 + 10 + n)
        if n < 2:
            assert p.consecutive_losses == n + 1
            assert p.last_loss_ms == T0 + 10 + n
    assert p.consecutive_losses == 0


def test_marking_is_at_bid_and_missing_bid_keeps_last_mark() -> None:
    p = Portfolio.with_cash(D("100"), T0)
    f = _fill(Side.BUY, "0.60", "10")
    _apply(p, f)
    p.mark({UP: D("0.55")}, T0 + 1)
    assert p.equity_usd == p.cash_usd + D("5.5")
    p.mark({UP: None}, T0 + 2)
    assert p.equity_usd == p.cash_usd + D("5.5")
    assert p.peak_equity_usd == D("100")  # initial equity is still the peak


def test_day_roll_resets_start_of_day_equity() -> None:
    p = Portfolio.with_cash(D("100"), T0)
    _apply(p, _fill(Side.BUY, "0.60", "10"))
    p.mark({UP: D("0.50")}, T0 + 1)
    assert p.start_of_day_equity_usd == D("100")
    next_day = T0 + 86_400_000
    p.mark({UP: D("0.50")}, next_day)
    assert p.start_of_day_equity_usd == p.equity_usd


def test_view_exposes_risk_inputs() -> None:
    p = Portfolio.with_cash(D("100"), T0)
    _apply(p, _fill(Side.BUY, "0.60", "10"))
    v = p.view(pending_buy_usd=D("2"), pending_sell_shares={UP: D("1")})
    assert v.positions[UP].shares == D("10")
    assert v.pending_buy_usd == D("2")
    assert v.cash_usd == p.cash_usd


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["buy", "sell", "settle"]),
            st.integers(min_value=1, max_value=99),  # price in cents
            st.integers(min_value=1, max_value=2000),  # shares in hundredths
        ),
        max_size=30,
    )
)
def test_cash_conservation_invariant(ops: list[tuple[str, int, int]]) -> None:
    """cash + open cost basis - realized PnL == initial cash, after any sequence."""
    initial = D("1000")
    p = Portfolio.with_cash(initial, T0)
    for n, (op, cents, hundredths) in enumerate(ops):
        price, shares = D(cents) / 100, D(hundredths) / 100
        if op == "buy":
            f = _fill(Side.BUY, str(price), str(shares), n=n)
            if f.price * f.shares + f.fee_usd <= p.cash_usd:
                _apply(p, f)
        elif op == "sell" and UP in p.positions:
            held = p.positions[UP].shares
            _apply(p, _fill(Side.SELL, str(price), str(min(shares, held)), n=n))
        elif op == "settle":
            p.settle(COND, "Up" if cents % 2 else "Down", T0 + n)
        assert p.cash_usd >= 0
        assert p.cash_usd + p.exposure_usd - p.realized_pnl_usd == initial
        assert all(pos.shares > 0 for pos in p.positions.values())


def test_rate_tracker_windows() -> None:
    r = RateTracker()
    r.record(T0, COND, OrderPurpose.ENTRY)
    r.record(T0 + 30_000, COND, OrderPurpose.EXIT)
    r.record(T0 + 61_000, "c2", OrderPurpose.ENTRY)
    v = r.view(T0 + 61_000)
    assert v.submissions_last_minute == 1
    assert v.submissions_last_hour == 2
    assert v.submissions_last_day == 2
    assert v.exits_last_minute == 1
    assert v.last_submit_ms_by_market == {COND: T0 + 30_000, "c2": T0 + 61_000}
    later = r.view(T0 + 86_400_000 + 1)
    assert later.submissions_last_day == 1
    assert later.exits_last_minute == 0
