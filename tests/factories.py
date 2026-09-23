"""Test factories. Values mirror the real fixtures in tests/fixtures (docs/research.md)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from polymarket_bot.config.risk_policy import RiskPolicy, policy_hash
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.decisions import TradeCandidate
from polymarket_bot.domain.market import (
    BookLevel,
    FeeSchedule,
    MarketDefinition,
    OrderBookSnapshot,
    OutcomeToken,
)
from polymarket_bot.domain.snapshot import MarketSnapshot, ReferenceView, TokenQuote
from polymarket_bot.domain.types import BotState, Side, TradingMode
from polymarket_bot.risk.engine import (
    EntryContext,
    HealthView,
    PortfolioView,
    PositionView,
    RateView,
    RiskEngine,
)

D = Decimal
T0 = 1790127600_000  # window start of a real market (btc-updown-5m-1790127600)
UP = "15329049429643092784575279708521316783620398456746213132371567405585638717474"
DOWN = "33215755676580564146338381586841381125623704954041225555979286173231064394422"
COND = "0xc77927db1e825c26dfadd89a4113dd0c4cc2609a2a3f9cb455546662ba074676"
CRYPTO_FEES = FeeSchedule(rate=D("0.07"), exponent=D("1"), taker_only=True)


def make_market(**kw: Any) -> MarketDefinition:
    base = MarketDefinition(
        market_id="4826443",
        condition_id=COND,
        slug="btc-updown-5m-1790127600",
        question="Bitcoin Up or Down - September 22, 9:40PM-9:45PM ET",
        tokens=(OutcomeToken(UP, "Up"), OutcomeToken(DOWN, "Down")),
        window_start_ms=T0,
        window_end_ms=T0 + 300_000,
        tick_size=D("0.01"),
        min_order_size=D("5"),
        fee_schedule=CRYPTO_FEES,
        neg_risk=False,
        rule_id="btc_5m_twap60_v3",
        accepting_orders=True,
        description_sha256="0" * 64,
    )
    return replace(base, **kw)


def make_book(
    token_id: str,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    received_ms: int,
    tick: str = "0.01",
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        bids=tuple(BookLevel(D(p), D(s)) for p, s in sorted(bids, key=lambda x: -D(x[0]))),
        asks=tuple(BookLevel(D(p), D(s)) for p, s in sorted(asks, key=lambda x: D(x[0]))),
        received_ms=received_ms,
        exchange_ms=received_ms - 50,
        book_hash="h",
        tick_size=D(tick),
    )


def quote_from_book(book: OrderBookSnapshot, outcome: str, now_ms: int) -> TokenQuote:
    return TokenQuote(
        token_id=book.token_id,
        outcome=outcome,
        best_bid=book.best_bid,
        best_ask=book.best_ask,
        mid=book.mid,
        spread=book.spread,
        bid_depth_usd=book.depth_usd("bid", D("0.05")),
        ask_depth_usd=book.depth_usd("ask", D("0.05")),
        last_trade_price=None,
        imbalance=book.imbalance(),
        book_received_ms=book.received_ms,
        book_exchange_ms=book.exchange_ms,
        book_age_ms=now_ms - book.received_ms,
        book_valid=True,
        book=book,
    )


def make_reference(now_ms: int, **kw: Any) -> ReferenceView:
    base = ReferenceView(
        spot=D("86700"),
        spot_observed_ms=now_ms - 500,
        spot_age_ms=500,
        twap=D("86690"),
        twap_observed_ms=now_ms - 500,
        twap_age_ms=500,
        price_to_beat=D("86635.83220274656"),
        price_to_beat_source="gamma+rtds",
        price_to_beat_verified=True,
        secondary_spot=D("86705"),
        secondary_age_ms=300,
        dispersion_bps=0.6,
    )
    return replace(base, **kw)


def make_snapshot(now_ms: int = T0 + 120_000, **kw: Any) -> MarketSnapshot:
    market = kw.pop("market", make_market())
    up_book = kw.pop(
        "up_book",
        make_book(
            UP, [("0.60", "200"), ("0.59", "300")], [("0.62", "150"), ("0.63", "400")], now_ms - 200
        ),
    )
    down_book = kw.pop(
        "down_book",
        make_book(
            DOWN,
            [("0.37", "150"), ("0.36", "400")],
            [("0.40", "200"), ("0.41", "300")],
            now_ms - 200,
        ),
    )
    base = MarketSnapshot(
        snapshot_id="snap-1",
        monotonic_ns=1,
        utc_ms=now_ms,
        market=market,
        quotes=(quote_from_book(up_book, "Up", now_ms), quote_from_book(down_book, "Down", now_ms)),
        reference=make_reference(now_ms),
        time_to_expiry_ms=market.window_end_ms - now_ms,
        time_since_start_ms=now_ms - market.window_start_ms,
        feeds_connected=True,
        stale_reasons=(),
    )
    return replace(base, **kw)


def make_candidate(**kw: Any) -> TradeCandidate:
    base = TradeCandidate(
        candidate_id="cand-1",
        snapshot_id="snap-1",
        condition_id=COND,
        market_slug="btc-updown-5m-1790127600",
        token_id=UP,
        outcome="Up",
        side=Side.BUY,
        fair_probability=0.72,
        probability_lower=0.69,
        probability_upper=0.75,
        executable_price=D("0.62"),
        worst_price=D("0.62"),
        effective_price=D("0.6417"),
        estimated_fee_usd=D("0.27"),
        estimated_slippage=D("0"),
        expected_edge=0.078,
        conservative_edge=0.045,
        worst_case_edge=0.045,
        liquidity_usd=D("93"),
        time_to_expiry_ms=180_000,
        size_shares=D("16.12"),
        notional_usd=D("10"),
        max_allowed_size_usd=D("10"),
        signal_version="test",
        model_version="test",
        feature_version="test",
        feature_timestamp_ms=T0 + 120_000,
        reason="test",
        confidence=0.8,
    )
    return replace(base, **kw)


def make_portfolio(**kw: Any) -> PortfolioView:
    base = PortfolioView(
        cash_usd=D("200"),
        equity_usd=D("200"),
        start_of_day_equity_usd=D("200"),
        peak_equity_usd=D("200"),
        positions={},
        pending_buy_usd=D("0"),
        pending_sell_shares={},
        consecutive_losses=0,
        last_loss_ms=None,
    )
    return replace(base, **kw)


def make_position(
    token_id: str = UP, shares: str = "16", cost: str = "10", cond: str = COND
) -> PositionView:
    return PositionView(
        token_id=token_id, condition_id=cond, shares=D(shares), cost_basis_usd=D(cost)
    )


def make_health(now_ms: int = T0 + 120_000, **kw: Any) -> HealthView:
    base = HealthView(
        market_stream_ok=True,
        reference_stream_ok=True,
        clock_drift_ms=20,
        last_reconciliation_ms=now_ms - 10_000,
        last_reconciliation_ok=True,
        watchdog_ok=True,
        unknown_orders=0,
        in_flight_orders=0,
        in_flight_tokens=frozenset(),
    )
    return replace(base, **kw)


def make_rates(**kw: Any) -> RateView:
    base = RateView(
        submissions_last_minute=0,
        submissions_last_hour=0,
        submissions_last_day=0,
        exits_last_minute=0,
        last_submit_ms_by_market={},
    )
    return replace(base, **kw)


def make_entry_ctx(now_ms: int = T0 + 120_000, **kw: Any) -> EntryContext:
    base = EntryContext(
        mode=TradingMode.PAPER,
        bot_state=BotState.PAPER,
        kill_switch_engaged=False,
        resolution_valid=True,
        resolution_detail="btc_5m_twap60_v3",
        portfolio=make_portfolio(),
        health=make_health(now_ms),
        rates=make_rates(),
        max_in_flight_orders=1,
    )
    return replace(base, **kw)


def make_engine(
    policy: RiskPolicy | None = None, now_ms: int = T0 + 120_000
) -> tuple[RiskEngine, SimulatedClock]:
    policy = policy or RiskPolicy()
    clock = SimulatedClock(now_ms)
    return RiskEngine(policy, policy_hash(policy), clock), clock
