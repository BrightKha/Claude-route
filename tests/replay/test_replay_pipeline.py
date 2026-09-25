"""End-to-end replay of SYNTHETIC sessions through the production pipeline."""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from polymarket_bot.app.replay_engine import ReplayRun, run_replay
from polymarket_bot.audit.audit_log import verify_audit_log
from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.config.loader import load_config
from polymarket_bot.data.replay import iter_messages, open_session
from polymarket_bot.domain.types import BotState, OrderPurpose
from polymarket_bot.research.synthetic import SynthParams, generate_session

ROOT = Path(__file__).resolve().parents[2]


def _config(**risk: Any) -> AppConfig:
    cfg = load_config(ROOT / "configs" / "paper.yaml")
    if not risk:
        return cfg
    data = cfg.model_dump(mode="json")
    data["risk"].update(risk)
    return AppConfig.model_validate(data)


async def _replay(session: Path, out: Path, config: AppConfig | None = None) -> ReplayRun:
    return await run_replay(config or _config(), session, out)


def _transitions(run: ReplayRun) -> list[tuple[str, str]]:
    rows = run.assembly.store.state_transitions(500)
    return [(r["from_state"], r["to_state"]) for r in reversed(rows)]


def _orders(run: ReplayRun) -> list[Any]:
    return run.assembly.store.load_orders()


async def test_synthetic_session_trades_end_to_end(synthetic_session: Path, tmp_path: Path) -> None:
    run = await _replay(synthetic_session, tmp_path)
    core, asm = run.assembly.core, run.assembly
    assert run.synthetic
    assert ("SYNCING", "PAPER") in _transitions(run)
    assert core.stats.entries_submitted > 0
    assert core.trade_log, "expected at least one closed trade"
    assert core.execution.violations == []
    assert not asm.kill_switch.is_engaged()
    # The paper venue's ledger and the bot's portfolio agree at the end.
    snapshot = await asm.paper.account_snapshot()
    assert snapshot.collateral_usd == core.portfolio.cash_usd
    assert snapshot.positions == {t: p.shares for t, p in core.portfolio.positions.items()}
    # Every reconciliation passed and the audit chain is intact.
    assert asm.store.last_reconciliation()["ok"] is True
    ok, n, _ = verify_audit_log(tmp_path / "audit.jsonl")
    assert ok and n > 10
    # Realised PnL equals the sum of closed-trade PnL.
    assert core.portfolio.realized_pnl_usd == sum(
        (t.trade.pnl_usd for t in core.trade_log), Decimal(0)
    )


async def test_replay_is_deterministic(synthetic_session: Path, tmp_path: Path) -> None:
    a = await _replay(synthetic_session, tmp_path / "a")
    b = await _replay(synthetic_session, tmp_path / "b")

    def fingerprint(run: ReplayRun) -> list[tuple[Any, ...]]:
        return [
            (o.intent.created_ms, o.intent.token_id, o.intent.limit_price, o.filled_shares)
            for o in _orders(run)
        ]

    assert fingerprint(a) == fingerprint(b)
    assert a.assembly.core.portfolio.cash_usd == b.assembly.core.portfolio.cash_usd


def _truncate(session: Path, dest: Path, cutoff_ms: int) -> Path:
    dest.mkdir(parents=True)
    shutil.copy(session / "session.json", dest / "session.json")
    with (dest / "part-0001.jsonl").open("w", encoding="utf-8") as out:
        for part in sorted(session.glob("part-*.jsonl")):
            for line in part.read_text(encoding="utf-8").splitlines():
                if json.loads(line)["t"] <= cutoff_ms:
                    out.write(line + "\n")
    return dest


async def test_no_lookahead_future_data_cannot_change_past_decisions(
    synthetic_session: Path, tmp_path: Path
) -> None:
    full = await _replay(synthetic_session, tmp_path / "full")
    cutoff = full.first_ms + (full.last_ms - full.first_ms) // 2
    truncated_session = _truncate(synthetic_session, tmp_path / "trunc_session", cutoff)
    part = await _replay(truncated_session, tmp_path / "part")
    margin = 2_000  # orders submitted just before the cutoff may be matched after it

    def before(run: ReplayRun) -> list[tuple[Any, ...]]:
        return [
            (o.intent.created_ms, o.intent.token_id, o.intent.side, o.intent.limit_price,
             o.intent.buy_amount_usd, o.intent.sell_shares, o.filled_shares)
            for o in _orders(run)
            if o.intent.created_ms < cutoff - margin
        ]  # fmt: skip

    assert before(full), "the first half of the session should contain orders"
    assert before(full) == before(part)


async def test_disconnects_halt_trading_then_recover(chaos_session: Path, tmp_path: Path) -> None:
    run = await _replay(chaos_session, tmp_path)
    transitions = _transitions(run)
    halts = run.assembly.store.recent_incidents(200)
    assert ("PAPER", "HALTED") in transitions
    assert ("HALTED", "SYNCING") in transitions  # automatic recovery once data is healthy
    assert transitions.count(("SYNCING", "PAPER")) >= 2
    assert any("market_stream" in json.dumps(i["body"]) for i in halts)
    assert run.assembly.core.execution.violations == []
    assert not run.assembly.kill_switch.is_engaged()


async def test_loss_limit_trips_kill_switch_and_stops_entries(
    synthetic_session: Path, tmp_path: Path
) -> None:
    run = await _replay(synthetic_session, tmp_path, _config(max_daily_loss_usd="0.05"))
    asm = run.assembly
    assert asm.kill_switch.is_engaged()
    assert asm.state.state is BotState.KILL_SWITCH
    engaged_at = next(
        i["ts_ms"] for i in asm.store.recent_incidents(200) if i["kind"] == "kill_switch"
    )
    late_entries = [
        o
        for o in _orders(run)
        if o.intent.purpose is OrderPurpose.ENTRY and o.intent.created_ms > engaged_at
    ]
    assert late_entries == []
    assert (tmp_path / "KILL_SWITCH").exists()


def _noisy_copy(session: Path, dest: Path) -> Path:
    """Copy a session, injecting duplicated and malformed frames."""
    dest.mkdir()
    shutil.copy(session / "session.json", dest / "session.json")
    seq = 0
    with (dest / "part-0001.jsonl").open("w", encoding="utf-8") as out:
        for part in sorted(session.glob("part-*.jsonl")):
            for i, line in enumerate(part.read_text(encoding="utf-8").splitlines()):
                env = json.loads(line)
                copies = [env]
                if i % 50 == 0 and env["src"] == "clob_ws" and env["kind"] == "ws_frame":
                    copies.append(dict(env))  # duplicated frame
                if i % 97 == 0:
                    copies.append({**env, "src": "clob_ws", "kind": "ws_frame", "data": "{bad"})
                if i % 89 == 0:
                    copies.append({**env, "src": "rtds", "kind": "ws_frame", "data": "[]garbage"})
                for c in copies:
                    seq += 1
                    out.write(json.dumps({**c, "seq": seq}) + "\n")
    return dest


async def test_malformed_and_duplicate_frames_do_not_break_the_pipeline(
    synthetic_session: Path, tmp_path: Path
) -> None:
    run = await _replay(_noisy_copy(synthetic_session, tmp_path / "noisy"), tmp_path / "out")
    assert run.assembly.hub.stats.malformed_frames > 0
    assert run.assembly.core.execution.violations == []
    assert ("SYNCING", "PAPER") in _transitions(run)


def test_regenerating_a_synthetic_session_replaces_it(tmp_path: Path) -> None:
    """Regression: a second ``make synth`` appended to the first session's part files.

    Replay then failed closed ("sequence not increasing") and ``make backtest`` broke.
    """
    params = SynthParams(windows=2, seed=3)
    first = generate_session(tmp_path, params, "s")
    n_first = sum(1 for _ in iter_messages(open_session(first)))
    again = generate_session(tmp_path, params, "s")
    assert again == first
    assert sum(1 for _ in iter_messages(open_session(again))) == n_first

    real = tmp_path / "recorded"
    real.mkdir()
    (real / "session.json").write_text(json.dumps({"synthetic": False}))
    (real / "part-0001.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="not a SYNTHETIC session"):
        generate_session(tmp_path, params, "recorded")
    assert (real / "part-0001.jsonl").read_text() == "{}\n"  # a real recording is never touched
