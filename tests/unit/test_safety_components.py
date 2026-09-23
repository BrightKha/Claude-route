"""Reconciler, watchdog, promotion gates, live lock, compliance."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from polymarket_bot.config.app_config import (
    AppConfig,
    ComplianceConfig,
    ReconciliationConfig,
    WatchdogConfig,
)
from polymarket_bot.config.settings import EnvSettings
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.orders import AccountSnapshot, OpenOrderView
from polymarket_bot.domain.types import BotState, Side
from polymarket_bot.lifecycle.state_machine import BotStateMachine
from polymarket_bot.promotion.gates import (
    Evidence,
    Stage,
    approval_phrase,
    evaluate_promotion,
)
from polymarket_bot.promotion.live_lock import evaluate_live_lock, expected_live_confirmation
from polymarket_bot.reconciliation.reconciler import LocalState, Reconciler
from polymarket_bot.security.compliance import attestation_check, interpret_geoblock_payload
from polymarket_bot.watchdog.health import HealthRegistry
from polymarket_bot.watchdog.watchdog import Watchdog

D = Decimal
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ------------------------------------------------------------------ reconciliation
def _remote(**kw):
    base = dict(
        collateral_usd=D("190"),
        positions={"tok": D("16")},
        open_orders=(),
        fetched_ms=1,
        source="test",
        complete=True,
    )
    base.update(kw)
    return AccountSnapshot(**base)


def _local(**kw):
    base = dict(
        positions={"tok": D("16")},
        cash_usd=D("190"),
        open_order_ids=frozenset(),
        known_order_ids=frozenset({"0xa"}),
    )
    base.update(kw)
    return LocalState(**base)


def test_reconciliation_ok():
    rep = Reconciler(ReconciliationConfig()).compare(_local(), _remote(), 5)
    assert rep.ok and rep.mismatches == ()


@pytest.mark.parametrize(
    ("local_kw", "remote_kw", "kind"),
    [
        ({}, dict(positions={"tok": D("10")}), "position"),
        ({}, dict(positions={"tok": D("16"), "other": D("5")}), "position"),
        ({}, dict(collateral_usd=D("150")), "cash"),
        ({}, dict(complete=False), "incomplete_remote"),
        (
            {},
            dict(
                open_orders=(
                    OpenOrderView("0xEXT", "tok", Side.BUY, D("0.5"), D("10"), D("0"), "LIVE"),
                )
            ),
            "external_order",
        ),
    ],
)
def test_reconciliation_critical_mismatches(local_kw, remote_kw, kind):
    rep = Reconciler(ReconciliationConfig()).compare(_local(**local_kw), _remote(**remote_kw), 5)
    assert not rep.ok
    assert kind in {m.kind for m in rep.critical}


def test_stale_local_open_order_is_warning_only():
    rep = Reconciler(ReconciliationConfig()).compare(
        _local(open_order_ids=frozenset({"0xa"})), _remote(), 5
    )
    assert rep.ok and rep.mismatches[0].kind == "stale_local_open_order"


def test_settled_tokens_can_be_ignored():
    rep = Reconciler(ReconciliationConfig()).compare(
        _local(positions={}, ignore_tokens=frozenset({"tok"})), _remote(), 5
    )
    assert rep.ok


# ------------------------------------------------------------------ watchdog
def _healthy_registry(now_ms):
    reg = HealthRegistry()
    reg.beat_loop()
    reg.market_stream(connected=True, msg_ms=now_ms - 100)
    reg.reference_stream(connected=True, msg_ms=now_ms - 100)
    reg.reconciliation(ts_ms=now_ms - 1000, ok=True)
    reg.clock_drift(10)
    return reg


def _watchdog(reg, clock, sm, cancels, incidents, kills):
    async def cancel_all():
        cancels.append(1)
        return True

    return Watchdog(
        WatchdogConfig(),
        reg,
        sm,
        clock,
        cancel_all=cancel_all,
        write_incident=lambda sev, kind, body: incidents.append((sev, kind)),
        engage_kill_switch=kills.append,
    )


async def test_watchdog_healthy_does_nothing():
    clock = SimulatedClock(10_000_000)
    sm = BotStateMachine(clock, initial=BotState.PAPER)
    cancels, incidents, kills = [], [], []
    wd = _watchdog(_healthy_registry(clock.now_ms()), clock, sm, cancels, incidents, kills)
    assert await wd.check_once() == []
    assert sm.state is BotState.PAPER and not cancels


async def test_watchdog_ws_down_halts_recoverably_and_cancels_once():
    clock = SimulatedClock(10_000_000)
    sm = BotStateMachine(clock, initial=BotState.PAPER)
    reg = _healthy_registry(clock.now_ms())
    reg.market_stream(connected=False)
    cancels, incidents, kills = [], [], []
    wd = _watchdog(reg, clock, sm, cancels, incidents, kills)
    await wd.check_once()
    assert sm.state is BotState.HALTED and not sm.manual_only
    assert cancels == [1] and incidents
    await wd.check_once()  # edge-triggered: no repeated action
    assert cancels == [1]
    assert {a.code for a in wd.recovery_blockers()} == {"market_stream_down"}


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda r, now: r.reconciliation(ts_ms=now, ok=False), "reconciliation_mismatch"),
        (lambda r, now: r.unknown_orders(1), "unknown_order_state"),
        (lambda r, now: r.clock_drift(9999), "clock_drift"),
        (lambda r, now: r.exception(), "unhandled_exception"),
    ],
)
async def test_watchdog_manual_halts(mutate, code):
    clock = SimulatedClock(10_000_000)
    sm = BotStateMachine(clock, initial=BotState.PAPER)
    reg = _healthy_registry(clock.now_ms())
    mutate(reg, clock.now_ms())
    wd = _watchdog(reg, clock, sm, [], [], [])
    anomalies = await wd.check_once()
    assert code in {a.code for a in anomalies}
    assert sm.state is BotState.HALTED and sm.manual_only


async def test_watchdog_detects_silent_feed():
    clock = SimulatedClock(10_000_000)
    sm = BotStateMachine(clock, initial=BotState.PAPER)
    reg = _healthy_registry(clock.now_ms())
    clock.advance_to(clock.now_ms() + 60_000)
    reg.reconciliation(ts_ms=clock.now_ms(), ok=True)
    codes = {a.code for a in await _watchdog(reg, clock, sm, [], [], []).check_once()}
    assert {"market_stream_silent", "reference_stream_silent"} <= codes


# ------------------------------------------------------------------ promotion
def _ev(kind, body, version="v1", ts=1):
    return Evidence(kind, version, body, ts)


def test_default_stage_is_research():
    st = evaluate_promotion([], [], strategy_version="v1", policy_hash="h" * 64)
    assert st.eligible_stage is Stage.RESEARCH and st.approved_stage is Stage.RESEARCH
    assert st.missing_for_next


def test_synthetic_evidence_never_counts():
    ev = [_ev("backtest", {"n_trades": 10_000, "synthetic": True})]
    assert (
        evaluate_promotion(ev, [], strategy_version="v1", policy_hash="h").eligible_stage
        is Stage.RESEARCH
    )
    ev = [_ev("backtest", {"n_trades": 10_000})]  # provenance unknown => synthetic
    assert (
        evaluate_promotion(ev, [], strategy_version="v1", policy_hash="h").eligible_stage
        is Stage.RESEARCH
    )


def _full_evidence(version="v1"):
    real = {"synthetic": False, "passed": True}
    return [
        _ev("backtest", {**real, "n_trades": 500}, version),
        _ev("oos", {**real, "n_trades": 300, "pnl_ci_low": 0.5, "brier_skill": 0.02}, version),
        _ev(
            "paper_session",
            {
                **real,
                "days": 20,
                "n_trades": 300,
                "open_incidents": 0,
                "reconciliation_failures": 0,
            },
            version,
        ),
        _ev("tests_passed", real, version),
        _ev("security_passed", real, version),
        _ev("kill_switch_drill", real, version),
        _ev("reconciliation_drill", real, version),
    ]


def test_small_live_requires_bound_operator_approval():
    ph = "p" * 64
    st = evaluate_promotion(_full_evidence(), [], strategy_version="v1", policy_hash=ph)
    assert st.eligible_stage is Stage.SMALL_LIVE and st.approved_stage is Stage.PAPER
    wrong_policy = {"stage": "SMALL_LIVE", "policy_hash": "x" * 64, "strategy_version": "v1",
                    "phrase": approval_phrase(Stage.SMALL_LIVE, "x" * 64, "v1")}  # fmt: skip
    st = evaluate_promotion(_full_evidence(), [wrong_policy], strategy_version="v1", policy_hash=ph)
    assert st.approved_stage is Stage.PAPER
    good = {"stage": "SMALL_LIVE", "policy_hash": ph, "strategy_version": "v1",
            "phrase": approval_phrase(Stage.SMALL_LIVE, ph, "v1")}  # fmt: skip
    st = evaluate_promotion(_full_evidence(), [good], strategy_version="v1", policy_hash=ph)
    assert st.approved_stage is Stage.SMALL_LIVE


def test_evidence_for_other_strategy_version_ignored():
    st = evaluate_promotion(_full_evidence("v0"), [], strategy_version="v1", policy_hash="h")
    assert st.eligible_stage is Stage.RESEARCH


# ------------------------------------------------------------------ live lock
def _lock(**overrides):
    ph = "p" * 64
    cfg = AppConfig.model_validate(
        {"mode": "live", "strategy": {"enabled": True}, "llm": {"allow_trading_without_llm": False}}
    )
    good = {"stage": "SMALL_LIVE", "policy_hash": ph, "strategy_version": cfg.strategy.version,
            "phrase": approval_phrase(Stage.SMALL_LIVE, ph, cfg.strategy.version)}  # fmt: skip
    promotion = evaluate_promotion(
        _full_evidence(cfg.strategy.version),
        [good],
        strategy_version=cfg.strategy.version,
        policy_hash=ph,
    )
    kwargs = dict(
        env=EnvSettings(
            trading_mode="live",
            live_trading_enabled=True,
            live_confirmation=expected_live_confirmation(ph),
        ),
        config=cfg,
        policy_hash=ph,
        hard_cap_clamps=[],
        promotion=promotion,
        compliance=(True, "attested CH; geoblock ok"),
        kill_switch_engaged=False,
        reconciliation_ok=True,
        market_data_ok=True,
        credential_names_present=["POLYMARKET_PRIVATE_KEY"],
        now_ms=1,
    )
    kwargs.update(overrides)
    return evaluate_live_lock(**kwargs)


def test_live_lock_passes_only_when_everything_passes():
    checks, auth = _lock()
    assert all(c.passed for c in checks), [c for c in checks if not c.passed]
    assert auth is not None


@pytest.mark.parametrize(
    "override",
    [
        dict(env=EnvSettings()),
        dict(
            env=EnvSettings(trading_mode="live", live_trading_enabled=True, live_confirmation="yes")
        ),
        dict(hard_cap_clamps=["max_position_usd clamped"]),
        dict(compliance=(False, "FR blocked")),
        dict(kill_switch_engaged=True),
        dict(reconciliation_ok=False),
        dict(market_data_ok=False),
        dict(credential_names_present=[]),
        dict(policy_hash="q" * 64),
    ],
)
def test_any_failing_precondition_keeps_live_locked(override):
    _, auth = _lock(**override)
    assert auth is None


def test_default_environment_never_passes_live_lock():
    checks, auth = _lock(env=EnvSettings(), config=AppConfig())
    assert auth is None
    assert sum(not c.passed for c in checks) >= 4


# ------------------------------------------------------------------ compliance
@pytest.mark.parametrize("code", ["FR", "us", "GB", "DE", "SG"])
def test_blocked_or_close_only_jurisdictions_refused(code):
    ok, detail = attestation_check(ComplianceConfig(), code)
    assert not ok and code.upper() in detail


def test_missing_or_invalid_attestation_refused():
    assert not attestation_check(ComplianceConfig(), "")[0]
    assert not attestation_check(ComplianceConfig(), "France")[0]


def test_geoblock_payload_interpretation_fail_closed():
    payload = json.loads((FIXTURES / "polymarket" / "geoblock_blocked_us.json").read_text())
    assert interpret_geoblock_payload(payload)[0] is False
    assert interpret_geoblock_payload({"blocked": False, "country": "CH"})[0] is True
    assert interpret_geoblock_payload({"country": "CH"})[0] is False
    assert interpret_geoblock_payload("blocked=false")[0] is False
