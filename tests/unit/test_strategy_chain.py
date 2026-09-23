"""Fair value model, features (no lookahead), fees, edge engine, exit engine."""

from __future__ import annotations

import json
import math
import random
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from polymarket_bot.config.app_config import (
    AppConfig,
    EdgeConfig,
    ExitPolicyConfig,
    FairValueConfig,
)
from polymarket_bot.config.risk_policy import RiskPolicy
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.market import FeeSchedule
from polymarket_bot.domain.types import BotState
from polymarket_bot.exits.engine import ExitEngine, HeldPosition
from polymarket_bot.features.btc5m import FEATURE_VERSION, FeatureVector, compute_features
from polymarket_bot.market.hub import MarketDataHub
from polymarket_bot.ports import RawMessage
from polymarket_bot.signals.edge import EdgeEngine, walk_asks
from polymarket_bot.strategies.btc_5m.fair_value import (
    FairValueEngine,
    FairValueEstimate,
    TwapGaussianModel,
    twap_moments,
)
from polymarket_bot.watchdog.health import HealthRegistry
from tests.factories import COND, CRYPTO_FEES, DOWN, T0, UP, make_book, make_snapshot

D = Decimal


# ------------------------------------------------------------------ fees vs official table
OFFICIAL_CRYPTO_TABLE = {  # docs.polymarket.com/trading/fees, 100 shares
    "0.01": "0.07", "0.05": "0.33", "0.10": "0.63", "0.15": "0.89", "0.20": "1.12",
    "0.25": "1.31", "0.30": "1.47", "0.35": "1.59", "0.40": "1.68", "0.45": "1.73",
    "0.50": "1.75", "0.55": "1.73", "0.60": "1.68", "0.65": "1.59", "0.70": "1.47",
    "0.75": "1.31", "0.80": "1.12", "0.85": "0.89", "0.90": "0.63", "0.95": "0.33", "0.99": "0.07",
}  # fmt: skip


@pytest.mark.parametrize(("price", "fee"), sorted(OFFICIAL_CRYPTO_TABLE.items()))
def test_fee_formula_matches_official_table(price, fee):
    got = CRYPTO_FEES.taker_fee(D("100"), D(price))
    assert got.quantize(D("0.01")) == D(fee)


def test_fee_rounding_is_conservative_and_zero_at_extremes():
    tiny = CRYPTO_FEES.taker_fee(D("0.01"), D("0.5"), conservative=True)
    assert tiny == D("0.00018")  # 0.000175 rounded UP to 5 decimals
    assert CRYPTO_FEES.fee_rate_at(D("0")) == 0 and CRYPTO_FEES.fee_rate_at(D("1")) == 0
    with pytest.raises(ValueError):
        FeeSchedule(rate=D("2"), exponent=D("1"))


# ------------------------------------------------------------------ TWAP moments
def test_twap_variance_formulas():
    mean, var = twap_moments(spot=100.0, tau_s=120, lookback_s=60, sigma=0.001, observed_avg=None)
    assert mean == 100.0
    assert var == pytest.approx((100 * 0.001) ** 2 * (120 - 40))
    mean, var = twap_moments(spot=100.0, tau_s=30, lookback_s=60, sigma=0.001, observed_avg=99.0)
    assert mean == pytest.approx(0.5 * 99 + 0.5 * 100)
    assert var == pytest.approx((100 * 0.001) ** 2 * 30**3 / (3 * 60**2))
    with pytest.raises(ValueError):
        twap_moments(spot=100.0, tau_s=30, lookback_s=60, sigma=0.001, observed_avg=None)


def test_twap_moments_continuous_at_boundary():
    _, v_left = twap_moments(spot=100.0, tau_s=60, lookback_s=60, sigma=0.001, observed_avg=100.0)
    _, v_right = twap_moments(
        spot=100.0, tau_s=59.999, lookback_s=60, sigma=0.001, observed_avg=100.0
    )
    assert v_left == pytest.approx(v_right, rel=1e-3)


def test_twap_moments_match_monte_carlo():
    rng = random.Random(3)
    spot, sigma, tau, lookback = 100.0, 0.0005, 150, 60
    finals = []
    for _ in range(4000):
        s = spot
        path = []
        for _t in range(tau):
            s += spot * sigma * rng.gauss(0, 1)
            path.append(s)
        finals.append(sum(path[-lookback:]) / lookback)
    mc_var = sum((x - spot) ** 2 for x in finals) / len(finals)
    _, var = twap_moments(spot=spot, tau_s=tau, lookback_s=lookback, sigma=sigma, observed_avg=None)
    assert mc_var == pytest.approx(var, rel=0.12)


def _fv(**kw) -> FeatureVector:
    base = dict(
        feature_version=FEATURE_VERSION,
        t_ms=0,
        tau_s=120.0,
        tau_eff_s=120.5,
        elapsed_s=180.0,
        spot=86700.0,
        price_to_beat=86635.0,
        log_distance_bps=7.5,
        sigma_per_sqrt_s=1.5e-4,
        vol_samples=300,
        observed_avg=None,
        observed_coverage=0.0,
        momentum_30s_bps=0.0,
        momentum_120s_bps=0.0,
        up_mid=0.6,
        down_mid=0.4,
        market_implied_up=0.6,
        up_spread=0.02,
        imbalance_up=0.0,
        dispersion_bps=1.0,
    )
    base.update(kw)
    return FeatureVector(**base)


MODEL = TwapGaussianModel(FairValueConfig())


@settings(max_examples=300, deadline=None)
@given(
    spot=st.floats(80_000, 95_000),
    ptb=st.floats(80_000, 95_000),
    tau=st.floats(61, 299),
    sigma=st.floats(0.3e-4, 5e-4),
)
def test_property_probability_bounds_are_ordered(spot, ptb, tau, sigma):
    est = MODEL.estimate(
        _fv(spot=spot, price_to_beat=ptb, tau_s=tau, tau_eff_s=tau, sigma_per_sqrt_s=sigma)
    )
    assert est.ok
    assert 0 < est.p_lower <= est.p_up <= est.p_upper < 1


@settings(max_examples=200, deadline=None)
@given(delta=st.floats(1, 300), tau=st.floats(61, 299))
def test_property_monotone_in_spot(delta, tau):
    lo = MODEL.estimate(_fv(spot=86635.0, tau_s=tau, tau_eff_s=tau))
    hi = MODEL.estimate(_fv(spot=86635.0 + delta, tau_s=tau, tau_eff_s=tau))
    assert hi.p_up >= lo.p_up


def test_symmetry_and_at_the_money():
    atm = MODEL.estimate(_fv(spot=86635.0))
    assert atm.p_up == pytest.approx(0.5, abs=1e-9)
    up = MODEL.estimate(_fv(spot=86735.0)).p_up
    down = MODEL.estimate(_fv(spot=86535.0)).p_up
    # Only approximately symmetric: the price-unit variance scales with the spot level.
    assert up == pytest.approx(1 - down, abs=2e-3)


def test_less_time_means_more_confidence():
    far = MODEL.estimate(_fv(spot=86700.0, tau_s=280, tau_eff_s=280)).p_up
    near = MODEL.estimate(_fv(spot=86700.0, tau_s=70, tau_eff_s=70)).p_up
    assert near > far > 0.5


def test_model_refuses_without_required_inputs():
    for kw, reason in [
        (dict(price_to_beat=None), "price to beat"),
        (dict(spot=None), "spot"),
        (dict(sigma_per_sqrt_s=None), "volatility"),
        (dict(vol_samples=5), "volatility"),
        (dict(tau_s=0.0, tau_eff_s=0.0), "window ended"),
        (dict(tau_s=30.0, tau_eff_s=30.0, observed_avg=None), "coverage"),
        (dict(tau_s=30.0, tau_eff_s=30.0, observed_avg=86700.0, observed_coverage=0.5), "coverage"),
    ]:
        est = MODEL.estimate(_fv(**kw))
        assert not est.ok and any(reason in r for r in est.reasons), kw


def test_model_error_widens_band_and_never_claims_certainty():
    narrow = TwapGaussianModel(FairValueConfig(model_error_bps=0.0)).estimate(_fv())
    wide = TwapGaussianModel(FairValueConfig(model_error_bps=10.0)).estimate(_fv())
    assert (wide.p_upper - wide.p_lower) > (narrow.p_upper - narrow.p_lower)
    extreme = MODEL.estimate(_fv(spot=95_000.0, tau_s=61, tau_eff_s=61))
    assert extreme.p_up <= 0.999


def test_for_outcome_maps_down_bounds():
    est = FairValueEstimate(True, 0.7, 0.65, 0.75, "v")
    assert est.for_outcome("Down") == pytest.approx((0.3, 0.25, 0.35))


# ------------------------------------------------------------------ features: no lookahead
def _rtds(topic, ts, value, window=None):
    payload = {"symbol": "btc/usd", "timestamp": ts, "value": value}
    if window:
        payload["full_accuracy_value"] = str(int(D(str(value)) * D(10) ** 18))
        payload["window_s"] = window
    return json.dumps({"topic": topic, "type": "update", "timestamp": ts + 20, "payload": payload})


def test_features_do_not_change_when_future_data_arrives():
    """Features at time t must be identical whether or not later data is ingested afterwards."""
    fix = Path(__file__).resolve().parents[1] / "fixtures" / "gamma"
    market = json.loads((fix / "btc_5m_twap60_market_open_1790127600.json").read_text())
    market[0]["events"][0]["eventMetadata"] = {"priceToBeat": 86635.0}

    def build(extra_future: bool):
        clock = SimulatedClock(T0 - 400_000)
        hub = MarketDataHub(AppConfig(), clock, HealthRegistry())
        hub.on_raw(RawMessage("gamma", "markets", market, clock.now_ms(), 0))
        hub.on_raw(RawMessage("rtds", "connection", {"state": "connected"}, clock.now_ms(), 0))
        for s in range(0, 520):
            t = T0 - 400_000 + s * 1000
            clock.advance_to(t)
            price = 86635.0 + 30 * math.sin(s / 17)
            hub.on_raw(
                RawMessage(
                    "rtds", "ws_frame", _rtds("crypto_prices_chainlink", t - 20, price), t, 0
                )
            )
            if t - 20 == T0 or s % 5 == 0:
                obs = T0 if t - 20 == T0 else t - 20
                hub.on_raw(
                    RawMessage(
                        "rtds",
                        "ws_frame",
                        _rtds("crypto_prices_twap_sixty", obs, 86635.0 if obs == T0 else price, 60),
                        t,
                        0,
                    )
                )
        snap = hub.snapshot(COND)
        feats = compute_features(snap, hub.reference, twap_lookback_s=60)
        if extra_future:
            for s in range(520, 560):
                t = T0 - 400_000 + s * 1000
                clock.advance_to(t)
                hub.on_raw(
                    RawMessage(
                        "rtds", "ws_frame", _rtds("crypto_prices_chainlink", t - 20, 99_999.0), t, 0
                    )
                )
        return feats

    a = build(False)
    b = build(True)
    assert a == b
    assert a.spot is not None and a.price_to_beat == 86635.0


# ------------------------------------------------------------------ edge engine
def _est(p, lo, hi):
    return FairValueEstimate(True, p, lo, hi, "test")


def test_walk_only_consumes_profitable_levels():
    book = make_book(UP, [("0.60", "100")], [("0.62", "10"), ("0.65", "10"), ("0.80", "1000")], 0)
    walk = walk_asks(
        book,
        budget_usd=D("100"),
        max_marginal_cost=D("0.70"),
        fees=CRYPTO_FEES,
        slippage_buffer=D("0.005"),
    )
    assert walk.worst_price == D("0.65")
    assert walk.shares == D("20")
    assert walk.notional == D("12.70")
    assert walk.vwap == D("0.635")


def test_walk_respects_budget():
    book = make_book(UP, [("0.60", "100")], [("0.62", "1000")], 0)
    walk = walk_asks(
        book,
        budget_usd=D("10"),
        max_marginal_cost=D("0.9"),
        fees=CRYPTO_FEES,
        slippage_buffer=D("0"),
    )
    assert walk.notional <= D("10")
    assert walk.shares == D("16.12")


def _snap_for_edge(now=T0 + 120_000):
    return make_snapshot(now)


def test_edge_engine_produces_one_passing_candidate_with_real_prices():
    engine = EdgeEngine(EdgeConfig(), RiskPolicy())
    cands = engine.candidates(
        _snap_for_edge(), _fv(), _est(0.80, 0.77, 0.83), resolution_valid=True
    )
    up = next(c for c in cands if c.outcome == "Up")
    down = next(c for c in cands if c.outcome == "Down")
    assert up.passes_filters, up.rejections
    assert not down.passes_filters
    assert up.executable_price == D("0.62")  # from the real ask, never the mid
    assert up.effective_price > up.executable_price
    assert up.conservative_edge <= up.expected_edge
    assert up.worst_case_edge <= up.conservative_edge + 1e-12
    assert up.notional_usd <= D("10")


@pytest.mark.parametrize(
    ("est", "fragment"),
    [
        (_est(0.66, 0.63, 0.69), "no profitable ask"),
        (_est(0.80, 0.50, 0.95), "uncertainty"),
        (FairValueEstimate(False, 0.5, 0.0, 1.0, "t", ("volatility not ready",)), "fair value"),
    ],
)
def test_edge_engine_rejects_marginal_or_uncertain(est, fragment):
    cands = EdgeEngine(EdgeConfig(), RiskPolicy()).candidates(
        _snap_for_edge(), _fv(), est, resolution_valid=True
    )
    assert not any(c.passes_filters for c in cands)
    assert any(fragment in r for c in cands for r in c.rejections)


def test_edge_engine_rejects_stale_and_unvalidated():
    snap = replace(_snap_for_edge(), stale_reasons=("rtds gap",))
    cands = EdgeEngine(EdgeConfig(), RiskPolicy()).candidates(
        snap, _fv(), _est(0.8, 0.77, 0.83), resolution_valid=False
    )
    reasons = [r for c in cands for r in c.rejections]
    assert any("stale" in r for r in reasons) and any("resolution" in r for r in reasons)


def test_edge_engine_rejects_wide_spread_and_thin_book():
    up = make_book(UP, [("0.50", "5")], [("0.62", "3")], T0 + 119_800)
    snap = make_snapshot(T0 + 120_000, up_book=up)
    cands = EdgeEngine(EdgeConfig(), RiskPolicy()).candidates(
        snap, _fv(), _est(0.8, 0.77, 0.83), resolution_valid=True
    )
    up_c = next(c for c in cands if c.outcome == "Up")
    assert any("spread" in r for r in up_c.rejections)
    assert any("liquidity" in r or "min order" in r for r in up_c.rejections)


# ------------------------------------------------------------------ exit engine
NOW = T0 + 120_000


def _pos(**kw):
    base = dict(
        token_id=UP,
        condition_id=COND,
        outcome="Up",
        shares=D("16"),
        avg_entry_price=D("0.64"),
        opened_ms=NOW - 60_000,
    )
    base.update(kw)
    return HeldPosition(**base)


def _exit(pos=None, snap=None, est=None, state=BotState.PAPER, kill=False, anomaly=False, cfg=None):
    return ExitEngine(cfg or ExitPolicyConfig()).evaluate(
        pos or _pos(),
        snap if snap is not None else make_snapshot(NOW),
        est if est is not None else _est(0.72, 0.69, 0.75),
        bot_state=state,
        kill_switch=kill,
        account_anomaly=anomaly,
        now_ms=NOW,
    )


def test_hold_when_nothing_triggers():
    ev = _exit()
    assert ev.action == "HOLD" and ev.signal is None


def test_no_value_exit_when_bid_net_of_fees_is_below_fair():
    # bid 0.60 nets 0.583 after the 0.07*p*(1-p) fee: below fair 0.61 -> holding is higher EV.
    assert _exit(est=_est(0.61, 0.58, 0.64)).action == "HOLD"


def test_converged_exit_never_below_fair_net_of_fees():
    ev = _exit(est=_est(0.58, 0.55, 0.61))  # bid 0.60 nets 0.583 >= 0.58 - tol
    assert ev.action == "EXIT", ev.reasons
    assert any("converged" in r for r in ev.reasons)
    fair = D("0.58")
    assert ev.signal.min_price >= fair + CRYPTO_FEES.fee_rate_at(fair) - D("0.01")
    assert ev.signal.min_price <= D("0.60")  # bid must be able to fill


def test_edge_negative_exit():
    ev = _exit(est=_est(0.50, 0.47, 0.53))
    assert ev.action == "EXIT" and any("edge negative" in r for r in ev.reasons)


def test_take_profit_exit():
    ev = _exit(pos=_pos(avg_entry_price=D("0.30")), est=_est(0.62, 0.57, 0.66))
    assert ev.action == "EXIT", ev.reasons
    assert any("take profit" in r for r in ev.reasons)
    lo = D("0.57")
    assert ev.signal.min_price >= lo + CRYPTO_FEES.fee_rate_at(lo)  # never below lower bound net


def test_take_profit_not_below_conservative_value():
    ev = _exit(pos=_pos(avg_entry_price=D("0.30")), est=_est(0.66, 0.60, 0.70))
    assert ev.action == "HOLD"  # bid net 0.583 < lower 0.60


NEAR_BID = _est(0.62, 0.60, 0.64)  # risk floor 0.58 <= bid 0.60, no value exit triggers


def test_kill_switch_risk_exit_and_hold_policy():
    ev = _exit(kill=True, state=BotState.KILL_SWITCH, est=NEAR_BID)
    assert ev.action == "EXIT", ev.reasons
    assert ev.signal.urgency == "urgent"
    assert ev.signal.min_price == D("0.58")
    hold = _exit(
        kill=True,
        state=BotState.KILL_SWITCH,
        est=NEAR_BID,
        cfg=ExitPolicyConfig(on_kill_switch="hold"),
    )
    assert hold.action == "HOLD"


def test_kill_switch_never_dumps_far_below_conservative_value():
    ev = _exit(kill=True, state=BotState.KILL_SWITCH)  # lower 0.69 vs bid 0.60
    assert ev.action == "HOLD"
    assert any("below floor" in r for r in ev.reasons)


def test_halt_risk_exit():
    assert _exit(state=BotState.HALTED, est=NEAR_BID).action == "EXIT"
    held = _exit(state=BotState.HALTED, est=NEAR_BID, cfg=ExitPolicyConfig(on_halt="hold"))
    assert held.action == "HOLD"


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        (dict(anomaly=True), "account anomaly"),
        (dict(pos=_pos(shares=D("3"))), "min order size"),
        (dict(snap=make_snapshot(T0 + 295_000)), "resolution imminent"),
        (dict(est=FairValueEstimate(False, 0.5, 0, 1, "t", ("x",))), "fair value unavailable"),
    ],
)
def test_holds_when_cannot_act_safely(kwargs, fragment):
    ev = _exit(**kwargs)
    assert ev.action == "HOLD"
    assert any(fragment in r for r in ev.reasons)


def test_stale_book_holds_even_under_kill_switch():
    snap = make_snapshot(NOW)
    stale = replace(snap.quotes[0], book_age_ms=60_000)
    ev = _exit(
        snap=replace(snap, quotes=(stale, snap.quotes[1])), kill=True, state=BotState.KILL_SWITCH
    )
    assert ev.action == "HOLD" and any("stale" in r for r in ev.reasons)


def test_max_holding_time_from_review_can_only_shorten():
    ev = _exit(pos=_pos(max_holding_s=30), est=NEAR_BID)
    assert ev.action == "EXIT", ev.reasons
    assert any("max holding" in r for r in ev.reasons)
    cfg = ExitPolicyConfig(max_holding_time_s=10)
    ev2 = _exit(pos=_pos(max_holding_s=10_000), cfg=cfg, est=NEAR_BID)
    assert any("10s" in r for r in ev2.reasons)


def test_invalidation_modes():
    est = _est(0.40, 0.36, 0.44)  # bid net 0.583 > upper 0.44 -> edge negative triggers first
    assert _exit(est=est).action == "EXIT"
    est = _est(0.33, 0.30, 0.36)  # upper 0.36 < entry 0.64 - margin -> invalidated
    snap = make_snapshot(
        NOW, up_book=make_book(UP, [("0.30", "100")], [("0.33", "100")], NOW - 200)
    )
    hold = _exit(snap=snap, est=est)
    assert hold.action == "HOLD" and any("invalidated" in r for r in hold.reasons)
    always = _exit(snap=snap, est=est, cfg=ExitPolicyConfig(invalidation_exit_mode="always"))
    assert always.action == "EXIT"


def test_no_exit_when_bid_below_floor():
    snap = make_snapshot(
        NOW, up_book=make_book(UP, [("0.05", "100")], [("0.62", "100")], NOW - 200)
    )
    ev = _exit(snap=snap, kill=True, state=BotState.KILL_SWITCH)
    assert ev.action == "HOLD" and any("below floor" in r for r in ev.reasons)


def test_down_position_uses_down_book():
    down_book = make_book(DOWN, [("0.47", "100")], [("0.49", "100")], NOW - 200)
    ev = _exit(
        pos=_pos(token_id=DOWN, outcome="Down", avg_entry_price=D("0.40")),
        snap=make_snapshot(NOW, down_book=down_book),
        est=_est(0.55, 0.52, 0.58),
    )
    assert ev.action == "EXIT", ev.reasons  # Down fair 0.45; bid 0.47 nets 0.4526 >= 0.44
    assert ev.signal.token_id == DOWN


def test_fair_value_engine_without_calibrator_is_baseline():
    eng = FairValueEngine(FairValueConfig())
    assert eng.estimate(_fv()) == eng.baseline.estimate(_fv())
    assert eng.version == FairValueConfig().model_version
