"""Risk Engine: every check can independently block an entry; sizing never exceeds caps."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from polymarket_bot.config.risk_policy import RiskPolicy
from polymarket_bot.domain.decisions import ExitSignal
from polymarket_bot.domain.types import BotState, OrderPurpose, Side, TradingMode
from polymarket_bot.risk.engine import ExitContext
from tests.factories import (
    COND,
    DOWN,
    T0,
    UP,
    make_book,
    make_candidate,
    make_engine,
    make_entry_ctx,
    make_health,
    make_portfolio,
    make_position,
    make_rates,
    make_reference,
    make_snapshot,
)

D = Decimal
NOW = T0 + 120_000


def _decide(candidate=None, snapshot=None, ctx=None, policy=None):
    engine, _ = make_engine(policy)
    return engine.evaluate_entry(
        candidate or make_candidate(), snapshot or make_snapshot(NOW), ctx or make_entry_ctx(NOW)
    )


def test_happy_path_is_allowed_and_fully_traced():
    decision = _decide()
    assert decision.allowed, decision.reasons
    assert decision.purpose is OrderPurpose.ENTRY
    assert decision.side is Side.BUY
    assert decision.limit_price == D("0.62")
    assert D("1") <= decision.max_size_usd <= D("10")
    assert decision.policy_hash and decision.risk_version
    assert all(c.passed for c in decision.checks)
    assert len(decision.checks) >= 35


EXPECTED_CHECK = {
    "kill_switch": "kill_switch",
    "trading_mode": "trading_mode",
    "bot_state_halted": "bot_state",
    "bot_state_mismatch_live": "bot_state",
    "resolution_invalid": "resolution_valid",
    "clock_drift_unknown": "clock_drift",
    "clock_drift_high": "clock_drift",
    "unknown_orders": "unknown_orders",
    "watchdog": "watchdog",
    "market_stream": "market_stream",
    "reference_stream": "reference_stream",
    "reconciliation_never": "reconciliation",
    "reconciliation_failed": "reconciliation",
    "reconciliation_stale": "reconciliation",
    "in_flight": "in_flight",
    "in_flight_token": "in_flight",
    "edge_low": "conservative_edge",
    "worst_case_edge_low": "worst_case_edge",
    "uncertainty_high": "uncertainty",
    "slippage": "slippage",
    "liquidity": "liquidity",
    "price_band_high": "entry_price_band",
    "price_band_low": "entry_price_band",
    "off_tick": "tick_size",
    "candidate_rejections": "candidate_filters",
    "sell_entry": "entry_side_buy",
    "wrong_market": "candidate_market",
    "bounds_insane": "probability_bounds_sane",
    "llm_multiplier_gt1": "llm_multiplier_range",
    "llm_multiplier_zero": "min_notional",
    "rate_minute": "rate_minute",
    "rate_hour": "rate_hour",
    "rate_day": "rate_day",
    "cooldown_same_market": "cooldown_same_market",
    "daily_loss": "loss_limits",
    "drawdown": "loss_limits",
    "consecutive_losses": "consecutive_losses",
    "cooldown_after_loss": "cooldown_after_loss",
    "balance": "min_notional",
    "exposure_limit": "exposure_limit",
    "opposite_outcome_held": "positions_per_market",
    "position_limit": "position_limit",
    "max_open_positions": "max_open_positions",
}

BLOCKING_CASES = {
    "kill_switch": dict(ctx=dict(kill_switch_engaged=True)),
    "trading_mode": dict(ctx=dict(mode=TradingMode.DISABLED)),
    "bot_state_halted": dict(ctx=dict(bot_state=BotState.HALTED)),
    "bot_state_mismatch_live": dict(ctx=dict(mode=TradingMode.LIVE, bot_state=BotState.PAPER)),
    "resolution_invalid": dict(ctx=dict(resolution_valid=False, resolution_detail="unknown rule")),
    "clock_drift_unknown": dict(health=dict(clock_drift_ms=None)),
    "clock_drift_high": dict(health=dict(clock_drift_ms=5000)),
    "unknown_orders": dict(health=dict(unknown_orders=1)),
    "watchdog": dict(health=dict(watchdog_ok=False)),
    "market_stream": dict(health=dict(market_stream_ok=False)),
    "reference_stream": dict(health=dict(reference_stream_ok=False)),
    "reconciliation_never": dict(health=dict(last_reconciliation_ms=None)),
    "reconciliation_failed": dict(health=dict(last_reconciliation_ok=False)),
    "reconciliation_stale": dict(health=dict(last_reconciliation_ms=NOW - 10 * 60_000)),
    "in_flight": dict(health=dict(in_flight_orders=1)),
    "in_flight_token": dict(health=dict(in_flight_tokens=frozenset({UP}))),
    "edge_low": dict(candidate=dict(conservative_edge=0.01)),
    "worst_case_edge_low": dict(candidate=dict(worst_case_edge=0.0)),
    "uncertainty_high": dict(candidate=dict(probability_lower=0.40, probability_upper=0.90)),
    "slippage": dict(candidate=dict(estimated_slippage=D("0.05"))),
    "liquidity": dict(candidate=dict(liquidity_usd=D("3"))),
    "price_band_high": dict(candidate=dict(worst_price=D("0.97"))),
    "price_band_low": dict(candidate=dict(worst_price=D("0.02"))),
    "off_tick": dict(candidate=dict(worst_price=D("0.625"))),
    "candidate_rejections": dict(candidate=dict(rejections=("spread too wide",))),
    "sell_entry": dict(candidate=dict(side=Side.SELL)),
    "wrong_market": dict(candidate=dict(condition_id="0x" + "1" * 64)),
    "bounds_insane": dict(candidate=dict(probability_lower=0.8, fair_probability=0.7)),
    "llm_multiplier_gt1": dict(ctx=dict(llm_size_multiplier=D("1.5"))),
    "llm_multiplier_zero": dict(ctx=dict(llm_size_multiplier=D("0"))),
    "rate_minute": dict(rates=dict(submissions_last_minute=2)),
    "rate_hour": dict(rates=dict(submissions_last_hour=20)),
    "rate_day": dict(rates=dict(submissions_last_day=120)),
    "cooldown_same_market": dict(rates=dict(last_submit_ms_by_market={COND: NOW - 1000})),
    "daily_loss": dict(portfolio=dict(equity_usd=D("165"), cash_usd=D("165"))),
    "drawdown": dict(portfolio=dict(peak_equity_usd=D("300"))),
    "consecutive_losses": dict(portfolio=dict(consecutive_losses=6)),
    "cooldown_after_loss": dict(portfolio=dict(last_loss_ms=NOW - 5_000)),
    "balance": dict(portfolio=dict(cash_usd=D("0.5"))),
    "exposure_limit": dict(portfolio=dict(pending_buy_usd=D("60"), cash_usd=D("200"))),
    "opposite_outcome_held": dict(portfolio=dict(positions={DOWN: make_position(DOWN)})),
    "position_limit": dict(portfolio=dict(positions={UP: make_position(UP, "40", "20")})),
    "max_open_positions": dict(
        portfolio=dict(
            positions={
                "a": make_position("a", cond="0x" + "a" * 64, cost="5"),
                "b": make_position("b", cond="0x" + "b" * 64, cost="5"),
            }
        )
    ),
}


@pytest.mark.parametrize("case", sorted(BLOCKING_CASES))
def test_each_check_blocks_independently(case):
    spec = BLOCKING_CASES[case]
    ctx = make_entry_ctx(NOW)
    if "health" in spec:
        ctx = replace(ctx, health=replace(make_health(NOW), **spec["health"]))
    if "rates" in spec:
        ctx = replace(ctx, rates=replace(make_rates(), **spec["rates"]))
    if "portfolio" in spec:
        ctx = replace(ctx, portfolio=replace(make_portfolio(), **spec["portfolio"]))
    if "ctx" in spec:
        ctx = replace(ctx, **spec["ctx"])
    candidate = make_candidate(**spec.get("candidate", {}))
    decision = _decide(candidate=candidate, ctx=ctx)
    assert not decision.allowed, f"{case} should block"
    assert decision.max_size_usd == 0
    failed = {c.name for c in decision.checks if not c.passed}
    assert EXPECTED_CHECK[case] in failed, (case, failed)


def test_every_blocking_case_has_an_expectation():
    assert set(EXPECTED_CHECK) == set(BLOCKING_CASES)


def test_stale_book_blocks():
    snap = make_snapshot(NOW)
    stale_quote = replace(snap.quotes[0], book_age_ms=5000)
    decision = _decide(snapshot=replace(snap, quotes=(stale_quote, snap.quotes[1])))
    assert not decision.allowed
    assert any(r.startswith("book_age") for r in decision.reasons)


def test_invalid_book_blocks():
    snap = make_snapshot(NOW)
    bad = replace(snap.quotes[0], book_valid=False)
    assert not _decide(snapshot=replace(snap, quotes=(bad, snap.quotes[1]))).allowed


def test_stale_reference_blocks():
    snap = replace(make_snapshot(NOW), reference=make_reference(NOW, twap_age_ms=60_000))
    assert not _decide(snapshot=snap).allowed


def test_missing_reference_blocks():
    snap = replace(make_snapshot(NOW), reference=make_reference(NOW, spot_age_ms=None, spot=None))
    assert not _decide(snapshot=snap).allowed


def test_unverified_price_to_beat_blocks():
    snap = replace(make_snapshot(NOW), reference=make_reference(NOW, price_to_beat_verified=False))
    assert not _decide(snapshot=snap).allowed


def test_snapshot_stale_reasons_block():
    snap = replace(make_snapshot(NOW), stale_reasons=("rtds gap",))
    assert not _decide(snapshot=snap).allowed


def test_feeds_disconnected_blocks():
    assert not _decide(snapshot=replace(make_snapshot(NOW), feeds_connected=False)).allowed


def test_wide_spread_blocks():
    up = make_book(UP, [("0.50", "200")], [("0.62", "200")], NOW - 100)
    assert not _decide(snapshot=make_snapshot(NOW, up_book=up)).allowed


def test_too_close_to_expiry_blocks():
    now = T0 + 280_000
    engine, _ = make_engine(now_ms=now)
    d = engine.evaluate_entry(make_candidate(), make_snapshot(now), make_entry_ctx(now))
    assert not d.allowed
    assert any(r.startswith("time_to_expiry") for r in d.reasons)


def test_too_early_in_window_blocks():
    now = T0 + 1_000
    engine, _ = make_engine(now_ms=now)
    d = engine.evaluate_entry(make_candidate(), make_snapshot(now), make_entry_ctx(now))
    assert any(r.startswith("time_since_start") for r in d.reasons)


def test_market_not_accepting_orders_blocks():
    snap = make_snapshot(NOW)
    snap = replace(snap, market=replace(snap.market, accepting_orders=False))
    assert not _decide(snapshot=snap).allowed


def test_size_is_min_of_caps_and_llm_can_only_reduce():
    full = _decide()
    halved = _decide(ctx=make_entry_ctx(NOW, llm_size_multiplier=D("0.5")))
    assert halved.allowed
    assert halved.max_size_usd <= full.max_size_usd / 2 + D("0.01")


def test_existing_position_limits_additional_size():
    ctx = make_entry_ctx(
        NOW, portfolio=make_portfolio(positions={UP: make_position(UP, "20", "15")})
    )
    d = _decide(ctx=ctx)
    assert d.allowed
    assert d.max_size_usd <= D("5")


def test_below_min_order_size_blocks():
    # Only $2 of room at 0.62 => 3.2 shares < 5 min order size.
    ctx = make_entry_ctx(
        NOW, portfolio=make_portfolio(positions={UP: make_position(UP, "28", "18")})
    )
    d = _decide(ctx=ctx)
    assert not d.allowed
    assert any(r.startswith("min_order_size") for r in d.reasons)


@settings(max_examples=300, deadline=None)
@given(
    cash=st.decimals(min_value=0, max_value=1000, places=2),
    pending=st.decimals(min_value=0, max_value=100, places=2),
    existing_cost=st.decimals(min_value=0, max_value=50, places=2),
    notional=st.decimals(min_value=0, max_value=200, places=2),
    mult=st.decimals(min_value=0, max_value=1, places=2),
)
def test_property_size_never_exceeds_any_cap(cash, pending, existing_cost, notional, mult):
    policy = RiskPolicy()
    positions = {UP: make_position(UP, "10", str(existing_cost))} if existing_cost > 0 else {}
    pf = make_portfolio(
        cash_usd=cash, equity_usd=D("200"), pending_buy_usd=pending, positions=positions
    )
    ctx = make_entry_ctx(NOW, portfolio=pf, llm_size_multiplier=mult)
    d = _decide(candidate=make_candidate(notional_usd=notional), ctx=ctx, policy=policy)
    exposure = existing_cost + pending
    if d.allowed:
        assert d.max_size_usd <= notional
        assert d.max_size_usd <= policy.max_order_size_usd
        assert d.max_size_usd + existing_cost <= policy.max_position_usd
        assert d.max_size_usd + exposure <= policy.max_total_exposure_usd
        assert d.max_size_usd <= cash - pending
        assert d.max_size_usd <= notional * mult + D("0.01")
        assert all(c.passed for c in d.checks)
    else:
        assert d.max_size_usd == 0


def test_loss_limit_breaches_reported_for_kill_switch():
    engine, _ = make_engine()
    assert engine.loss_limit_breaches(make_portfolio()) == []
    breaches = engine.loss_limit_breaches(make_portfolio(equity_usd=D("160")))
    assert any("daily loss" in b for b in breaches)
    dd = engine.loss_limit_breaches(
        make_portfolio(peak_equity_usd=D("400"), start_of_day_equity_usd=D("200"))
    )
    assert any("drawdown" in b for b in dd)


# ------------------------------------------------------------------------- exits
def _exit_ctx(**kw):
    base = ExitContext(
        mode=TradingMode.PAPER,
        bot_state=BotState.PAPER,
        kill_switch_engaged=False,
        portfolio=make_portfolio(positions={UP: make_position(UP, "16", "10")}),
        health=make_health(NOW),
        rates=make_rates(),
        exit_allowed_in_kill_switch=True,
    )
    return replace(base, **kw)


def _signal(**kw):
    base = ExitSignal(
        token_id=UP,
        condition_id=COND,
        reasons=("convergence",),
        urgency="normal",
        shares=D("16"),
        min_price=D("0.60"),
        timestamp_ms=NOW,
    )
    return replace(base, **kw)


def test_exit_allowed_reduce_only():
    engine, _ = make_engine()
    d = engine.evaluate_exit(_signal(), make_snapshot(NOW), _exit_ctx())
    assert d.allowed, d.reasons
    assert d.side is Side.SELL and d.purpose is OrderPurpose.EXIT
    assert d.max_shares == D("16") and d.limit_price == D("0.60")


@pytest.mark.parametrize(
    ("signal_kw", "ctx_kw"),
    [
        (dict(shares=D("20")), {}),  # more than held
        (dict(min_price=None), {}),  # cannot price safely
        (dict(min_price=D("0.605")), {}),  # off tick
        (dict(shares=D("3")), {}),  # below min order size
        ({}, dict(bot_state=BotState.DISABLED)),
        (
            {},
            dict(
                kill_switch_engaged=True,
                bot_state=BotState.KILL_SWITCH,
                exit_allowed_in_kill_switch=False,
            ),
        ),
        ({}, dict(health=make_health(NOW, unknown_orders=1))),
        ({}, dict(health=make_health(NOW, in_flight_tokens=frozenset({UP})))),
        ({}, dict(rates=make_rates(exits_last_minute=10))),
    ],
)
def test_exit_blocked(signal_kw, ctx_kw):
    engine, _ = make_engine()
    d = engine.evaluate_exit(_signal(**signal_kw), make_snapshot(NOW), _exit_ctx(**ctx_kw))
    assert not d.allowed
    assert d.max_shares == 0


def test_exit_allowed_when_halted_by_policy():
    engine, _ = make_engine()
    assert engine.evaluate_exit(
        _signal(), make_snapshot(NOW), _exit_ctx(bot_state=BotState.HALTED)
    ).allowed
    strict, _ = make_engine(RiskPolicy(allow_exits_when_halted=False))
    assert not strict.evaluate_exit(
        _signal(), make_snapshot(NOW), _exit_ctx(bot_state=BotState.HALTED)
    ).allowed


def test_exit_requires_fresh_book():
    engine, _ = make_engine()
    snap = make_snapshot(NOW)
    stale = replace(snap.quotes[0], book_age_ms=10_000)
    d = engine.evaluate_exit(_signal(), replace(snap, quotes=(stale, snap.quotes[1])), _exit_ctx())
    assert not d.allowed


def test_exit_without_snapshot_denied():
    engine, _ = make_engine()
    assert not engine.evaluate_exit(_signal(), None, _exit_ctx()).allowed


def test_exit_respects_pending_sells():
    engine, _ = make_engine()
    pf = make_portfolio(
        positions={UP: make_position(UP, "16", "10")}, pending_sell_shares={UP: D("16")}
    )
    assert not engine.evaluate_exit(_signal(), make_snapshot(NOW), _exit_ctx(portfolio=pf)).allowed
