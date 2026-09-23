from __future__ import annotations

from decimal import Decimal

import pytest

from polymarket_bot.config.risk_policy import RiskPolicy, clamp_to_hard_caps, policy_hash
from polymarket_bot.risk.hard_caps import HARD_CAPS, SMALL_LIVE_CAPS

D = Decimal


def test_default_policy_is_within_hard_caps():
    effective, notes = clamp_to_hard_caps(RiskPolicy())
    assert notes == []
    assert effective == RiskPolicy()


def test_oversized_policy_is_clamped_and_reported():
    reckless = RiskPolicy(
        max_position_usd=D("100000"),
        max_total_exposure_usd=D("1000000"),
        max_order_size_usd=D("50000"),
        max_daily_loss_usd=D("99999"),
        max_open_positions=100,
        min_conservative_edge=D("0.0001"),
        max_spread=D("0.5"),
        max_trades_per_minute=1000,
    )
    effective, notes = clamp_to_hard_caps(reckless)
    assert effective.max_position_usd == HARD_CAPS["max_position_usd"].value
    assert effective.max_total_exposure_usd == HARD_CAPS["max_total_exposure_usd"].value
    assert effective.max_order_size_usd == HARD_CAPS["max_order_size_usd"].value
    assert effective.max_open_positions == int(HARD_CAPS["max_open_positions"].value)
    assert isinstance(effective.max_open_positions, int)
    assert effective.min_conservative_edge == HARD_CAPS["min_conservative_edge"].value
    assert effective.max_spread == HARD_CAPS["max_spread"].value
    assert len(notes) >= 8


def test_small_live_caps_are_tighter():
    effective, notes = clamp_to_hard_caps(RiskPolicy(), small_live=True)
    for name, cap in SMALL_LIVE_CAPS.items():
        assert D(str(getattr(effective, name))) <= cap
    assert notes


def test_every_hard_cap_refers_to_a_policy_field():
    fields = set(RiskPolicy.model_fields)
    assert set(HARD_CAPS) <= fields
    assert set(SMALL_LIVE_CAPS) <= fields


def test_hard_caps_are_immutable():
    with pytest.raises(TypeError):
        HARD_CAPS["max_position_usd"] = None  # type: ignore[index]


def test_policy_hash_changes_with_any_field():
    base = policy_hash(RiskPolicy())
    assert policy_hash(RiskPolicy(max_spread=D("0.03"))) != base
    assert policy_hash(RiskPolicy()) == base


def test_policy_rejects_unknown_fields():
    with pytest.raises(ValueError):
        RiskPolicy.model_validate({"max_position_usd": 10, "max_leverage": 100})


def test_policy_is_frozen():
    with pytest.raises(ValueError):
        RiskPolicy().max_position_usd = D("1")  # type: ignore[misc]
