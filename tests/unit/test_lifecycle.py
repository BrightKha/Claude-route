"""State machine, kill switch, audit chain and state store."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from polymarket_bot.audit.audit_log import AuditLog, verify_audit_log
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.orders import Fill, OrderIntent, OrderRecord
from polymarket_bot.domain.types import BotState, OrderPurpose, OrderStatus, OrderType, Side
from polymarket_bot.lifecycle.kill_switch import (
    RESET_CONFIRMATION_PHRASE,
    KillSwitch,
    KillSwitchError,
)
from polymarket_bot.lifecycle.state_machine import (
    BotStateMachine,
    LiveAuthorization,
    TransitionError,
)
from polymarket_bot.storage.sqlite_store import ProposalInbox, StateStore

D = Decimal


@pytest.fixture
def clock():
    return SimulatedClock(1_000_000)


def _sm(clock, state=BotState.DISABLED):
    return BotStateMachine(clock, initial=state)


# ------------------------------------------------------------------ state machine
def test_normal_paper_startup_path(clock):
    sm = _sm(clock)
    sm.transition(BotState.INITIALIZING, "boot")
    sm.transition(BotState.SYNCING, "sync")
    sm.transition(BotState.PAPER, "ready")
    assert sm.can_open_positions()


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (BotState.DISABLED, BotState.PAPER),
        (BotState.DISABLED, BotState.LIVE),
        (BotState.PAPER, BotState.LIVE),
        (BotState.HALTED, BotState.PAPER),
        (BotState.HALTED, BotState.LIVE),
        (BotState.KILL_SWITCH, BotState.PAPER),
        (BotState.DEAD, BotState.DISABLED),
    ],
)
def test_illegal_transitions_rejected(clock, start, target):
    sm = _sm(clock, start)
    with pytest.raises(TransitionError):
        sm.transition(target, "nope", manual=True)


def test_live_requires_authorization_and_cannot_be_forged(clock):
    sm = _sm(clock, BotState.SYNCING)
    with pytest.raises(TransitionError):
        sm.transition(BotState.LIVE, "no auth")
    with pytest.raises(TransitionError):
        LiveAuthorization("h", "v", 0, (), _token=object())
    assert not sm.is_live()


def test_kill_switch_state_requires_manual_exit(clock):
    sm = _sm(clock, BotState.PAPER)
    sm.transition(BotState.KILL_SWITCH, "loss")
    assert sm.manual_only
    with pytest.raises(TransitionError):
        sm.transition(BotState.DISABLED, "auto")
    sm.transition(BotState.DISABLED, "operator", manual=True)
    assert sm.state is BotState.DISABLED


def test_manual_halt_blocks_auto_recovery(clock):
    sm = _sm(clock, BotState.PAPER)
    sm.halt("reconciliation mismatch", manual_only=True)
    with pytest.raises(TransitionError):
        sm.transition(BotState.SYNCING, "auto-recover")
    sm.transition(BotState.SYNCING, "operator resume", manual=True)


def test_recoverable_halt_allows_resync(clock):
    sm = _sm(clock, BotState.PAPER)
    sm.halt("ws down")
    sm.transition(BotState.SYNCING, "reconnected")
    sm.transition(BotState.PAPER, "resynced")


def test_halt_escalation_never_deescalates(clock):
    sm = _sm(clock, BotState.PAPER)
    sm.halt("a", manual_only=True)
    sm.halt("b", manual_only=False)
    assert sm.manual_only


def test_listeners_receive_changes(clock):
    sm = _sm(clock)
    seen = []
    sm.subscribe(seen.append)
    sm.transition(BotState.INITIALIZING, "boot")
    assert seen and seen[0].to_state is BotState.INITIALIZING


# ------------------------------------------------------------------ kill switch
@pytest.fixture
def ks_env(tmp_path, clock):
    store = StateStore(tmp_path / "state.sqlite")
    audit = AuditLog(tmp_path / "audit.jsonl", clock, fsync=False)
    sm = _sm(clock, BotState.PAPER)
    ks = KillSwitch(tmp_path, store, sm, audit, clock)
    return ks, sm, store, tmp_path


def test_kill_switch_engage_persists_everywhere(ks_env):
    ks, sm, store, tmp = ks_env
    ks.engage("daily loss", source="risk")
    assert ks.is_engaged()
    assert sm.state is BotState.KILL_SWITCH
    assert (tmp / "KILL_SWITCH").exists()
    assert store.kill_switch_state()[0]
    assert store.recent_incidents()[0]["kind"] == "kill_switch"


def test_kill_switch_survives_restart(ks_env, clock):
    ks, _, store, tmp = ks_env
    ks.engage("x", source="test")
    sm2 = _sm(clock, BotState.DISABLED)
    ks2 = KillSwitch(tmp, store, sm2, AuditLog(tmp / "audit.jsonl", clock, fsync=False), clock)
    ks2.sync_from_persistence()
    assert sm2.state is BotState.KILL_SWITCH


def test_operator_sentinel_file_engages(ks_env):
    ks, _, _, tmp = ks_env
    (tmp / "KILL_SWITCH").write_text("operator pressed the button")
    assert ks.is_engaged()
    assert "operator" in ks.reason()


def test_kill_switch_reset_requires_phrase_and_note(ks_env):
    ks, sm, _, tmp = ks_env
    ks.engage("x", source="test")
    with pytest.raises(KillSwitchError):
        ks.reset(operator="alice", confirmation="yes", investigation_note="looked at it closely")
    with pytest.raises(KillSwitchError):
        ks.reset(operator="alice", confirmation=RESET_CONFIRMATION_PHRASE, investigation_note="ok")
    ks.reset(
        operator="alice",
        confirmation=RESET_CONFIRMATION_PHRASE,
        investigation_note="root cause identified: feed outage",
    )
    assert not ks.is_engaged()
    assert sm.state is BotState.DISABLED  # never straight back to trading
    assert not (tmp / "KILL_SWITCH").exists()


# ------------------------------------------------------------------ audit chain
def test_audit_chain_verifies_and_detects_tampering(tmp_path, clock):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, clock, fsync=False)
    for i in range(5):
        log.append("event", {"i": i, "amount": D("1.5")})
    assert verify_audit_log(path) == (True, 5, "")

    lines = path.read_text().splitlines()
    rec = json.loads(lines[2])
    rec["payload"]["i"] = 99
    tampered = [*lines[:2], json.dumps(rec, separators=(",", ":"), sort_keys=True), *lines[3:]]
    path.write_text("\n".join(tampered) + "\n")
    ok, _, err = verify_audit_log(path)
    assert not ok and "line 3" in err

    path.write_text("\n".join([lines[0], *lines[2:]]) + "\n")  # deletion
    assert not verify_audit_log(path)[0]


def test_audit_chain_resumes_after_restart(tmp_path, clock):
    path = tmp_path / "audit.jsonl"
    AuditLog(path, clock, fsync=False).append("a", {})
    AuditLog(path, clock, fsync=False).append("b", {})
    assert verify_audit_log(path) == (True, 2, "")


# ------------------------------------------------------------------ state store
def _intent(decision_id="rd-1"):
    return OrderIntent(
        intent_id=f"oi-{decision_id}",
        decision_id=decision_id,
        condition_id="0x" + "c" * 64,
        market_slug="m",
        token_id="tok",
        outcome="Up",
        side=Side.BUY,
        order_type=OrderType.FAK,
        limit_price=D("0.62"),
        buy_amount_usd=D("10"),
        sell_shares=None,
        purpose=OrderPurpose.ENTRY,
        created_ms=1,
    )


def test_order_write_ahead_and_duplicate_decision_rejected(tmp_path):
    store = StateStore(tmp_path / "s.sqlite")
    store.insert_order_intent(_intent())
    with pytest.raises(Exception, match="UNIQUE"):
        store.insert_order_intent(replace(_intent(), intent_id="other"))
    orders = store.load_orders(only_open=True)
    assert len(orders) == 1 and orders[0].status is OrderStatus.INTENT
    rec = OrderRecord(
        orders[0].intent, OrderStatus.FILLED, "0xabc", D("16"), D("9.92"), D("0.27"), 5
    )
    store.update_order(rec)
    assert store.load_orders(only_open=True) == []
    assert store.load_orders()[0].avg_price == D("0.62")


def test_fills_are_idempotent(tmp_path):
    store = StateStore(tmp_path / "s.sqlite")
    fill = Fill(
        "f1",
        "oi",
        "0xabc",
        "0x" + "c" * 64,
        "tok",
        "Up",
        Side.BUY,
        D("0.62"),
        D("16"),
        D("0.27"),
        1,
        "taker",
        "paper",
    )
    assert store.insert_fill(fill)
    assert not store.insert_fill(fill)
    assert store.load_fills() == [fill]


def test_read_only_store_refuses_writes(tmp_path):
    path = tmp_path / "s.sqlite"
    StateStore(path).set_meta("x", "1")
    ro = StateStore(path, read_only=True)
    assert ro.get_meta("x") == "1"
    with pytest.raises(PermissionError):
        ro.set_meta("x", "2")


def test_bot_state_persisted_with_history(tmp_path):
    store = StateStore(tmp_path / "s.sqlite")
    store.save_bot_state(
        state="HALTED", reason="ws", manual_only=False, ts_ms=5, from_state="PAPER"
    )
    assert store.load_bot_state() == ("HALTED", "ws", False)
    assert store.state_transitions()[0]["to_state"] == "HALTED"


def test_proposal_inbox_roundtrip(tmp_path):
    inbox = ProposalInbox(tmp_path / "inbox.sqlite")
    inbox.submit(
        proposal_id="p1", created_ms=1, source="mcp", kind="request_trade", payload={"x": 1}
    )
    assert inbox.pending()[0]["payload"] == {"x": 1}
    inbox.mark("p1", "REJECTED", "risk", 2)
    assert inbox.pending() == []
    assert inbox.get("p1")["status"] == "REJECTED"
