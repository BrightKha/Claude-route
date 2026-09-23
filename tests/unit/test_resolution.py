"""BTC 5m resolution adapter against REAL archived Polymarket payloads."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from polymarket_bot.strategies.btc_5m.resolution import (
    RULES_BY_ID,
    description_hash,
    iter_event_markets,
    resolved_outcome,
    slug_for_window,
    validate_market,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "gamma"
ENABLED = ("btc_5m_twap60_v3",)


def _load(name):
    return json.loads((FIX / name).read_text())


def _pairs(name):
    return list(iter_event_markets(_load(name)))


def test_rule_hashes_match_verbatim_fixture_texts():
    _twap_event, twap_market = _pairs("btc_5m_twap_events_2026-09-23.json")[0]
    assert (
        description_hash(twap_market["description"])
        == RULES_BY_ID["btc_5m_twap60_v3"].description_sha256
    )
    _spot_event, spot_market = _pairs("btc_5m_spot_v1_event_2026-04-30.json")[0]
    assert (
        description_hash(spot_market["description"])
        == RULES_BY_ID["btc_5m_spot_v1"].description_sha256
    )


def test_open_twap60_market_is_tradable():
    event, market = _pairs("btc_5m_twap60_market_open_1790127600.json")[0]
    res = validate_market(event, market, enabled_rule_ids=ENABLED)
    assert res.ok, res.reasons
    md = res.market
    assert md.rule_id == "btc_5m_twap60_v3"
    assert md.tokens[0].outcome == "Up" and md.tokens[1].outcome == "Down"
    assert md.window_end_ms - md.window_start_ms == 300_000
    assert md.slug == slug_for_window(md.window_start_ms)
    assert md.fee_schedule.rate == Decimal("0.07")
    assert md.tick_size == Decimal("0.01") and md.min_order_size == Decimal("5")


@pytest.mark.parametrize(("event", "market"), _pairs("btc_5m_twap_events_2026-09-23.json"))
def test_real_resolved_markets_outcome_consistent_with_rule(event, market):
    res = validate_market(event, market, enabled_rule_ids=ENABLED, require_tradable=False)
    assert res.ok, res.reasons
    out = resolved_outcome(event, market, res.rule)
    assert out.winner in ("Up", "Down")
    assert out.consistent_with_rule, out.detail
    if res.price_to_beat is not None and res.final_price is not None:
        assert out.winner == ("Up" if res.final_price >= res.price_to_beat else "Down")


def test_closed_markets_are_not_tradable():
    event, market = _pairs("btc_5m_twap_events_2026-09-23.json")[0]
    res = validate_market(event, market, enabled_rule_ids=ENABLED)
    assert not res.ok
    assert "market closed" in res.reasons


def test_old_spot_rule_recognised_but_not_enabled():
    event, market = _pairs("btc_5m_spot_v1_event_2026-04-30.json")[0]
    settle = validate_market(event, market, enabled_rule_ids=ENABLED, require_tradable=False)
    assert settle.ok and settle.rule.rule_id == "btc_5m_spot_v1"
    assert resolved_outcome(event, market, settle.rule).winner == "Up"
    live = validate_market(event, market, enabled_rule_ids=ENABLED)
    assert any("not enabled" in r for r in live.reasons)


def test_zombie_market_rejected():
    event, market = _pairs("btc_5m_zombie_event_2025-12-19.json")[0]
    res = validate_market(event, market, enabled_rule_ids=("btc_5m_spot_v1", "btc_5m_twap60_v3"))
    assert not res.ok
    assert "order book disabled" in res.reasons
    assert any("effective period" in r for r in res.reasons)


MUTATIONS = {
    "reworded description": lambda m: m.update(description=m["description"] + " "),
    "other resolution source": lambda m: m.update(resolutionSource="https://example.com"),
    "config lookback changed": lambda m: m["cryptoMarketConfig"].update(twapLookbackSeconds=30),
    "config missing": lambda m: m.pop("cryptoMarketConfig"),
    "outcomes swapped": lambda m: m.update(outcomes='["Down", "Up"]'),
    "three tokens": lambda m: m.update(clobTokenIds='["1111111111", "2222222222", "3333333333"]'),
    "duplicate tokens": lambda m: m.update(clobTokenIds='["1111111111", "1111111111"]'),
    "neg risk": lambda m: m.update(negRisk=True),
    "window 15m": lambda m: m.update(endDate="2026-09-23T01:55:00Z"),
    "slug mismatch": lambda m: m.update(slug="btc-updown-5m-1790127300"),
    "fees unknown": lambda m: m.pop("feeSchedule"),
    "fees disabled": lambda m: m.update(feesEnabled=False),
    "absurd fee": lambda m: m["feeSchedule"].update(rate=0.9),
    "bad tick": lambda m: m.update(orderPriceMinTickSize=0.02),
    "bad condition": lambda m: m.update(conditionId="0x1234"),
    "outcomes not json": lambda m: m.update(outcomes="Up,Down"),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_any_deviation_from_registered_rule_is_rejected(name):
    event, market = copy.deepcopy(_pairs("btc_5m_twap60_market_open_1790127600.json")[0])
    MUTATIONS[name](market)
    res = validate_market(event, market, enabled_rule_ids=ENABLED)
    assert not res.ok, name
    assert res.market is None


def test_unresolved_or_ambiguous_payouts_return_no_winner():
    event, market = copy.deepcopy(_pairs("btc_5m_twap_events_2026-09-23.json")[0])
    rule = RULES_BY_ID["btc_5m_twap60_v3"]
    market["umaResolutionStatus"] = "proposed"
    assert resolved_outcome(event, market, rule).winner is None
    market["umaResolutionStatus"] = "resolved"
    market["outcomePrices"] = '["0.5", "0.5"]'
    assert resolved_outcome(event, market, rule).winner is None


def test_payout_contradicting_rule_is_flagged():
    event, market = copy.deepcopy(_pairs("btc_5m_twap_events_2026-09-23.json")[1])
    rule = RULES_BY_ID["btc_5m_twap60_v3"]
    prices = json.loads(market["outcomePrices"])
    market["outcomePrices"] = json.dumps(list(reversed(prices)))
    out = resolved_outcome(event, market, rule)
    assert out.winner is not None and not out.consistent_with_rule


def test_slug_requires_alignment():
    with pytest.raises(ValueError):
        slug_for_window(1790127601_000)
