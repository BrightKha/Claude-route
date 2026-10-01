"""Halt journal (cause / component / condition / recovery) and price-to-beat evidence."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from polymarket_bot.app.build import assemble
from polymarket_bot.app.cli import EXIT_OK, main
from polymarket_bot.audit.audit_log import AuditLog
from polymarket_bot.config.app_config import AppConfig, FairValueConfig
from polymarket_bot.config.loader import load_config
from polymarket_bot.data.recorder import SessionRecorder
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.types import BotState, TradingMode
from polymarket_bot.lifecycle.halt_journal import HaltJournal
from polymarket_bot.lifecycle.state_machine import BotStateMachine
from polymarket_bot.monitoring.halt_history import halt_history
from polymarket_bot.ports import RawMessage
from polymarket_bot.strategies.btc_5m.ptb_validation import (
    PriceToBeatValidator,
    compute_stats,
    gate,
)
from polymarket_bot.watchdog.health import HealthRegistry
from polymarket_bot.watchdog.watchdog import Watchdog

ROOT = Path(__file__).resolve().parents[2]
CONFIG = str(ROOT / "configs" / "paper.yaml")
FIX = ROOT / "tests" / "fixtures"
T0 = 1790127600_000
COND = "0xc77927db1e825c26dfadd89a4113dd0c4cc2609a2a3f9cb455546662ba074676"
PTB = 86635.83220274656
D = Decimal


def _audit(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------- halt journal
async def test_watchdog_halt_and_recovery_are_journaled_with_their_cause(tmp_path: Path) -> None:
    clock = SimulatedClock(10_000_000)
    audit = AuditLog(tmp_path / "audit.jsonl", clock, fsync=False)
    sm = BotStateMachine(clock, initial=BotState.PAPER)
    sm.subscribe(HaltJournal(audit))
    reg = HealthRegistry()
    reg.beat_loop()
    reg.market_stream(connected=True, msg_ms=clock.now_ms())
    reg.market_heartbeat(clock.now_ms())  # heartbeats only: no book event ever
    reg.reference_stream(connected=True, msg_ms=clock.now_ms())
    reg.reference_event("spot", clock.now_ms())
    reg.reference_event("twap60", clock.now_ms())
    reg.reconciliation(ts_ms=clock.now_ms(), ok=True)
    reg.clock_drift(5)

    async def cancel_all() -> bool:
        return True

    wd = Watchdog(
        AppConfig().watchdog,
        reg,
        sm,
        clock,
        cancel_all=cancel_all,
        write_incident=lambda *a: None,
        engage_kill_switch=lambda r: None,
    )
    await wd.check_once()
    assert sm.state is BotState.HALTED
    clock.advance_to(clock.now_ms() + 4_000)
    sm.transition(BotState.SYNCING, "auto recovery: blockers cleared")
    clock.advance_to(clock.now_ms() + 2_000)
    sm.transition(BotState.PAPER, "synced")

    events = {r["kind"]: r["payload"] for r in _audit(tmp_path / "audit.jsonl")}
    halt = events["halt"]
    assert halt["component"] == "watchdog" and halt["category"] == "feed"
    assert halt["condition"]["anomalies"][0]["code"] == "market_stream_silent"
    assert halt["condition"]["health"]["market_heartbeat_age_s"] == 0.0
    assert halt["condition"]["health"]["market_book_event_age_s"] is None
    assert events["recovery_started"]["halt_id"] == halt["halt_id"]
    assert events["recovery_started"]["halt_cause"] == halt["cause"]  # never hidden
    assert events["recovered"]["downtime_ms"] == 6_000

    (row,) = halt_history(tmp_path / "audit.jsonl")
    assert row["component"] == "watchdog" and row["category"] == "feed"
    assert row["recovered_at"] is not None and row["downtime_s"] == 6.0


def test_halt_history_reads_logs_written_before_the_journal(tmp_path: Path) -> None:
    """Old logs only have state_change reasons; the watchdog incident adds the condition."""
    clock = SimulatedClock(5_000)
    audit = AuditLog(tmp_path / "audit.jsonl", clock, fsync=False)

    def change(frm: str, to: str, reason: str, ts: int) -> None:
        clock.advance_to(ts)
        audit.append(
            "state_change",
            {
                "change": {
                    "from_state": frm,
                    "to_state": to,
                    "reason": reason,
                    "manual_only": False,
                    "ts_ms": ts,
                }
            },
        )

    cause = "watchdog: market_stream_down(disconnected); clock_drift_unknown(no measurement)"
    change("PAPER", "HALTED", cause, 10_000)
    change("HALTED", "SYNCING", "auto recovery: blockers cleared", 40_000)
    change("SYNCING", "PAPER", "synced", 41_000)
    incident = {"ts_ms": 10_000, "kind": "watchdog_halt", "body": {"anomalies": ["x"]}}
    (row,) = halt_history(tmp_path / "audit.jsonl", [incident])
    assert row["cause"] == cause and row["category"] == "feed"
    assert row["component"].startswith("unknown")
    assert row["condition"] == {"anomalies": ["x"], "detail": None}
    assert row["downtime_s"] == 31.0


def test_kill_switch_on_loss_limit_is_a_risk_halt(tmp_path: Path) -> None:
    asm = assemble(
        load_config(CONFIG), mode=TradingMode.PAPER, clock=SimulatedClock(T0), data_dir=tmp_path
    )
    asm.core.start()
    asm.kill_switch.engage("loss limit: daily loss 12% >= 10%", source="core")
    halts = [r["payload"] for r in _audit(tmp_path / "audit.jsonl") if r["kind"] == "halt"]
    assert halts[-1]["category"] == "risk" and halts[-1]["component"] == "kill_switch:core"


# ---------------------------------------------------------------- price-to-beat evidence
def _obs(n: int, diff: float | None = 0.0, *, synthetic: bool = False) -> dict[str, Any]:
    return {
        "slug": f"btc-updown-5m-{n}",
        "window_start_ms": n * 300_000,
        "official": "84000",
        "stream": None if diff is None else "84000",
        "diff_bps": diff,
        "first_seen_ms": n * 300_000 + 420_000,
        "source": "paper",
        "synthetic": synthetic,
    }


def test_validation_stats() -> None:
    obs = [_obs(1, 0.0), _obs(2, 0.004), _obs(3, None), _obs(4, 0.5), _obs(5, 0.0, synthetic=True)]
    stats = compute_stats(obs, max_diff_bps=0.01)
    assert stats.n_official == 4 and stats.n_windows == 3 and stats.missing_stream == 1
    assert stats.match_count == 2
    assert stats.max_abs_diff_bps == 0.5
    assert stats.p95_abs_diff_bps == 0.5
    assert stats.mean_abs_diff_bps == pytest.approx(0.168)
    assert stats.mismatches == ("btc-updown-5m-4",)
    assert set(stats.as_dict()) >= {
        "N_WINDOWS",
        "MATCH_COUNT",
        "MAX_ABS_DIFF_BPS",
        "P95_ABS_DIFF_BPS",
        "MEAN_ABS_DIFF_BPS",
    }


def test_gate_is_off_by_default_and_needs_100_matching_windows() -> None:
    assert FairValueConfig().stream_price_to_beat_policy == "off"
    with pytest.raises(ValidationError):
        FairValueConfig(stream_price_to_beat_min_windows=99)
    many = [_obs(n) for n in range(150)]
    off = FairValueConfig()
    assert gate(compute_stats(many, max_diff_bps=0.01), off) == (False, ["policy off"])
    on = FairValueConfig(stream_price_to_beat_policy="evidence_gated")
    assert gate(compute_stats(many[:99], max_diff_bps=0.01), on)[0] is False
    assert gate(compute_stats(many[:100], max_diff_bps=0.01), on) == (True, [])
    one_bad = [*many[:100], _obs(500, 0.3)]
    assert gate(compute_stats(one_bad, max_diff_bps=0.01), on) == (False, ["1 mismatching windows"])
    synthetic = PriceToBeatValidator(on, source="replay", synthetic=True)
    synthetic.seed(many)
    assert synthetic.allowed() is False  # synthetic runs never use the stream value


def _raw(src: str, kind: str, payload: object, t: int) -> RawMessage:
    return RawMessage(src, kind, payload, t, t * 1_000_000)


def _market(ptb: float | None, *, closed: bool = False) -> object:
    markets = json.loads((FIX / "gamma" / "btc_5m_twap60_market_open_1790127600.json").read_text())
    if ptb is not None:
        markets[0]["events"][0]["eventMetadata"] = {"priceToBeat": ptb}
    return markets


def _twap_at_start() -> str:
    payload = {
        "symbol": "btc/usd",
        "timestamp": T0,
        "value": PTB,
        "full_accuracy_value": str(int(D(str(PTB)) * D(10) ** 18)),
        "window_s": 60,
    }
    msg = {
        "topic": "crypto_prices_twap_sixty",
        "type": "update",
        "timestamp": T0,
        "payload": payload,
    }
    return json.dumps(msg)


@pytest.mark.parametrize(("policy", "verified"), [("off", False), ("evidence_gated", True)])
def test_policy_uses_the_stream_value_only_with_evidence(
    tmp_path: Path, policy: str, verified: bool
) -> None:
    data = load_config(CONFIG).model_dump(mode="json")
    data["fair_value"]["stream_price_to_beat_policy"] = policy
    config = AppConfig.model_validate(data)
    asm = assemble(
        config, mode=TradingMode.PAPER, clock=SimulatedClock(T0 + 60_000), data_dir=tmp_path
    )
    assert asm.hub.ptb_validator is not None
    asm.hub.ptb_validator.seed([_obs(n) for n in range(100)])
    asm.hub.on_raw(_raw("gamma", "markets", _market(None), T0 + 60_000))
    asm.hub.on_raw(_raw("rtds", "ws_frame", _twap_at_start(), T0 + 500))
    ref = asm.hub.snapshot(COND).reference
    assert ref.price_to_beat_verified is verified
    assert ref.price_to_beat_source == "rtds"


async def test_mismatch_halts_when_policy_on_and_is_an_incident_when_off(tmp_path: Path) -> None:
    data = load_config(CONFIG).model_dump(mode="json")
    data["fair_value"]["stream_price_to_beat_policy"] = "evidence_gated"
    config = AppConfig.model_validate(data)
    clock = SimulatedClock(T0 + 60_000)
    asm = assemble(config, mode=TradingMode.PAPER, clock=clock, data_dir=tmp_path)
    asm.core.start()
    asm.hub.on_raw(_raw("gamma", "markets", _market(None), T0 + 60_000))
    asm.hub.on_raw(_raw("rtds", "ws_frame", _twap_at_start(), T0 + 500))
    clock.advance_to(T0 + 420_000)
    asm.hub.on_raw(_raw("gamma", "markets", _market(PTB + 50), T0 + 420_000))  # 5.8 bps off
    await asm.core.step()
    assert asm.state.state is BotState.HALTED and asm.state.manual_only
    kinds = [i["kind"] for i in asm.store.recent_incidents(10)]
    assert "price_to_beat_mismatch" in kinds
    (obs,) = asm.store.ptb_observations()
    assert obs["diff_bps"] == pytest.approx(5.77, abs=0.01)


def test_ptb_validate_accumulates_evidence_from_recordings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec_root = tmp_path / "recordings"
    clock = SimulatedClock(T0 - 60_000)
    recorder = SessionRecorder(rec_root, "paper-test", clock)
    recorder.record_raw(_raw("gamma", "markets", _market(None), T0 - 60_000))
    recorder.record_raw(_raw("rtds", "ws_frame", _twap_at_start(), T0 + 500))
    recorder.record_raw(_raw("gamma", "markets", _market(PTB), T0 + 420_000))
    recorder.close()
    synth = SessionRecorder(rec_root, "synthetic-x", clock, synthetic=True)
    synth.record_raw(_raw("gamma", "markets", _market(PTB), T0))
    synth.close()

    data = tmp_path / "data"
    args = ["--config", CONFIG, "--data-dir", str(data), "ptb-validate", "--input", str(rec_root)]
    assert main(args) == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    stats = report["PRICE_TO_BEAT_VALIDATION"]
    assert stats["N_WINDOWS"] == 1 and stats["MATCH_COUNT"] == 1
    assert stats["MAX_ABS_DIFF_BPS"] == 0.0
    assert report["gate_open"] is False and "policy off" in report["gate_closed_because"]
    sessions = {s["session"]: s for s in report["sessions"]}
    assert sessions["synthetic-x"]["synthetic"] is True
    assert sessions["synthetic-x"]["observations"] == 0
    assert main(args) == EXIT_OK  # idempotent: the same window is never counted twice
    assert json.loads(capsys.readouterr().out)["observations_added"] == 0
