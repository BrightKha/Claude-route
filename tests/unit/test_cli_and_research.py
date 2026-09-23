"""CLI safety behaviour, MCP proposal processing in the core, research metrics."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from polymarket_bot.app.build import assemble
from polymarket_bot.app.cli import EXIT_FAIL, EXIT_LOCKED, EXIT_OK, main
from polymarket_bot.config.loader import load_config
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.types import TradingMode
from polymarket_bot.lifecycle.kill_switch import RESET_CONFIRMATION_PHRASE
from polymarket_bot.research.metrics import (
    bootstrap_sum_ci,
    brier,
    brier_skill,
    bucket,
    calibration_table,
    group_by,
    log_loss,
    max_drawdown,
)
from polymarket_bot.research.walk_forward import fit_logistic
from tests.factories import T0

ROOT = Path(__file__).resolve().parents[2]
CONFIG = str(ROOT / "configs" / "paper.yaml")
LIVE_CONFIG = str(ROOT / "configs" / "live.example.yaml")


@pytest.fixture(autouse=True)
def _no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "POLYMARKET_PRIVATE_KEY",
        "ANTHROPIC_API_KEY",
        "LIVE_TRADING_ENABLED",
        "TRADING_MODE",
        "LIVE_CONFIRMATION",
        "OPERATOR_JURISDICTION",
        "BOT_DATA_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------- CLI
def test_synth_replay_and_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    session = tmp_path / "sessions" / "s1"
    assert main(["synth", "--out", str(session), "--windows", "3", "--seed", "2"]) == EXIT_OK
    assert json.loads((session / "session.json").read_text())["synthetic"] is True
    data = tmp_path / "data"
    args = ["--config", CONFIG, "--data-dir", str(data)]
    assert main([*args, "replay", "--input", str(session), "--out", str(data)]) == EXIT_OK
    out = capsys.readouterr().out
    summary = json.loads(out[out.index("{") :])
    assert summary["synthetic"] is True and summary["violations"] == []
    assert main([*args, "status"]) == EXIT_OK
    status = json.loads(capsys.readouterr().out)
    assert status["audit_log"]["valid"] is True


def test_kill_switch_cli_engage_and_guarded_reset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--config", CONFIG, "--data-dir", str(tmp_path)]
    assert main([*args, "kill-switch", "engage", "--reason", "drill"]) == EXIT_OK
    assert (tmp_path / "KILL_SWITCH").exists()
    wrong = [*args, "kill-switch", "reset", "--operator", "op", "--note", "looked at logs"]
    assert main([*wrong, "--confirm", "yes"]) == EXIT_FAIL
    assert (tmp_path / "KILL_SWITCH").exists()
    ok = [*wrong, "--confirm", RESET_CONFIRMATION_PHRASE]
    assert main(ok) == EXIT_OK
    assert not (tmp_path / "KILL_SWITCH").exists()


def test_live_is_locked_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--config", LIVE_CONFIG, "--data-dir", str(tmp_path)]
    assert main([*args, "live-readiness"]) == EXIT_LOCKED
    out = capsys.readouterr().out
    assert "[FAIL] env TRADING_MODE=live" in out
    assert "[FAIL] live credentials present" in out
    assert main([*args, "--mode", "live"]) == EXIT_LOCKED
    assert "LIVE LOCKED" in capsys.readouterr().err


def test_live_stays_locked_even_with_env_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_CONFIRMATION", "I-ACCEPT-LIVE-RISK-wrong")
    monkeypatch.setenv("OPERATOR_JURISDICTION", "FR")
    args = ["--config", LIVE_CONFIG, "--data-dir", str(tmp_path), "--mode", "live"]
    assert main(args) == EXIT_LOCKED
    err = capsys.readouterr().err
    assert "LIVE_CONFIRMATION" in err
    assert "blocked list" in err  # FR is on Polymarket's restricted list
    assert "promotion approved" in err


def test_promotion_approval_requires_exact_phrase(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--config", CONFIG, "--data-dir", str(tmp_path)]
    bad = [*args, "promotion", "approve", "--stage", "SMALL_LIVE", "--phrase", "yes"]
    assert main([*bad, "--operator", "op"]) == EXIT_FAIL
    assert "phrase must be exactly APPROVE-STAGE-SMALL_LIVE-" in capsys.readouterr().err
    assert main([*args, "promotion", "status"]) == EXIT_OK
    status = json.loads(capsys.readouterr().out)
    assert status["approved_stage"] == "RESEARCH"
    assert status["eligible_stage"] == "RESEARCH"
    assert any("non-synthetic backtest" in m for m in status["missing_for_next"])


def test_synthetic_backtest_evidence_never_promotes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = tmp_path / "s"
    assert main(["synth", "--out", str(session), "--windows", "3", "--seed", "4"]) == EXIT_OK
    args = ["--config", CONFIG, "--data-dir", str(tmp_path / "d")]
    report = tmp_path / "report"
    cmd = [*args, "backtest", "--input", str(session), "--report", str(report)]
    assert main([*cmd, "--record-evidence"]) == EXIT_OK
    text = (report / "report.md").read_text()
    assert "SYNTHETIC DATA" in text
    capsys.readouterr()
    assert main([*args, "promotion", "status"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["eligible_stage"] == "RESEARCH"


# ---------------------------------------------------------------- MCP proposals in the core
async def test_proposals_are_validated_by_the_core(tmp_path: Path) -> None:
    config = load_config(CONFIG)
    clock = SimulatedClock(T0)
    asm = assemble(config, mode=TradingMode.PAPER, clock=clock, data_dir=tmp_path, with_inbox=True)
    core = asm.core
    core.start()
    inbox = core.d.inbox
    assert inbox is not None
    payload = {
        "market_slug": "btc-updown-5m-1790127600",
        "outcome": "Up",
        "max_notional_usd": "5",
        "rationale": "x",
    }
    inbox.submit(proposal_id="p1", created_ms=T0, source="mcp", kind="trade", payload=payload)
    inbox.submit(proposal_id="p2", created_ms=T0, source="mcp", kind="close", payload=payload)
    inbox.submit(
        proposal_id="p3", created_ms=T0 - 10**6, source="mcp", kind="trade", payload=payload
    )
    inbox.submit(proposal_id="p4", created_ms=T0, source="mcp", kind="withdraw", payload={})
    await core.step()
    statuses = {p: inbox.get(p)["status"] for p in ("p1", "p2", "p3", "p4")}
    assert statuses == {"p1": "REJECTED", "p2": "REJECTED", "p3": "EXPIRED", "p4": "REJECTED"}
    assert "unknown or unvalidated market" in inbox.get("p1")["status_reason"]
    assert asm.store.load_orders() == []


# ---------------------------------------------------------------- research metrics
def test_scoring_rules() -> None:
    perfect = [(1.0, 1), (0.0, 0)]
    assert brier(perfect) == 0
    assert brier([(0.5, 1), (0.5, 0)]) == 0.25
    assert log_loss([(0.5, 1)]) == pytest.approx(np.log(2))
    assert brier_skill(0.2, 0.25) == pytest.approx(0.2)
    assert brier([]) is None and brier_skill(None, 0.2) is None
    table = calibration_table([(0.15, 0), (0.12, 1), (0.95, 1)])
    assert [r["n"] for r in table] == [2, 1]


def test_drawdown_bootstrap_and_buckets() -> None:
    # Largest absolute and largest relative drawdowns (120 -> 90 is 25%).
    assert max_drawdown([100, 120, 90, 130, 100]) == (30, 0.25)
    lo, hi = bootstrap_sum_ci([1.0, -1.0, 2.0, 0.5], samples=500) or (0, 0)
    assert lo <= 2.5 <= hi
    assert bucket(0.04, (0.0, 0.03, 0.05)) == "[0.03,0.05)"
    assert bucket(None, (0.0, 1.0)) == "unknown"
    assert bucket(9.0, (0.0, 1.0)) == ">=1"
    rows = group_by([1, 2, 3, 4], lambda x: "even" if x % 2 == 0 else "odd", float)
    assert rows["even"] == {"n": 2, "pnl": 6, "mean": 3}


def test_logistic_fit_recovers_signal() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(4000, 1))
    y = (rng.random(4000) < 1 / (1 + np.exp(-(0.5 + 2.0 * x[:, 0])))).astype(float)
    w = fit_logistic(x, y, l2=0.0)
    assert w[0] == pytest.approx(0.5, abs=0.15)
    assert w[1] == pytest.approx(2.0, abs=0.2)


def test_required_llm_without_client_blocks_entries(tmp_path: Path) -> None:
    from polymarket_bot.config.app_config import AppConfig  # noqa: PLC0415
    from tests.factories import make_candidate  # noqa: PLC0415

    data = load_config(CONFIG).model_dump(mode="json")
    data["llm"]["mode"] = "required"
    config = AppConfig.model_validate(data)
    asm = assemble(config, mode=TradingMode.PAPER, clock=SimulatedClock(T0), data_dir=tmp_path)
    reviewer = asm.core.d.reviewer
    assert reviewer is not None
    verdict = reviewer.verdict_for(make_candidate(conservative_edge=0.2), T0)
    assert verdict.status == "not_reviewed"
    assert verdict.allowed is False
