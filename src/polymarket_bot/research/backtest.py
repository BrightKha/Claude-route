"""Backtest = replay through the production pipeline + a metrics report.

The report states its provenance up front. A SYNTHETIC session produces a
report that is useful to validate plumbing and metrics code only; the
promotion gates ignore it (``Evidence.synthetic``).

"With vs without Claude" cannot be measured in replay (Claude is not called and
recorded reviews are not replayed yet); the report says so instead of guessing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from polymarket_bot.app.core import TradeLog
from polymarket_bot.app.replay_engine import ReplayRun, run_replay
from polymarket_bot.config.app_config import AppConfig
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

EDGE_BUCKETS = (0.0, 0.03, 0.05, 0.08, 0.12)
TTE_BUCKETS_S = (0.0, 60.0, 120.0, 180.0, 240.0)
VOL_BUCKETS_BPS = (0.0, 0.7, 1.0, 1.5, 2.5)


def _f(x: Decimal | float | None) -> float | None:
    return None if x is None else float(x)


def _execution_quality(db: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT o.purpose, o.status, o.filled_shares, o.filled_notional, o.fees_usd, "
            "o.limit_price, c.body AS cand FROM orders o "
            "LEFT JOIN risk_decisions d ON d.decision_id = o.decision_id "
            "LEFT JOIN candidates c ON c.candidate_id = d.candidate_id"
        ).fetchall()
    finally:
        conn.close()
    entries = [r for r in rows if r["purpose"] == "ENTRY"]
    exits = [r for r in rows if r["purpose"] == "EXIT"]
    filled = [r for r in entries if Decimal(r["filled_shares"]) > 0]
    slippages: list[float] = []
    for r in filled:
        if r["cand"] is None:
            continue
        cand = json.loads(r["cand"]).get("candidate", {})
        planned = cand.get("executable_price")
        if planned is None:
            continue
        vwap = Decimal(r["filled_notional"]) / Decimal(r["filled_shares"])
        slippages.append(float(vwap - Decimal(str(planned))))
    return {
        "entry_orders": len(entries),
        "entry_orders_filled": len(filled),
        "entry_fill_ratio": len(filled) / len(entries) if entries else None,
        "exit_orders": len(exits),
        "exit_orders_filled": sum(1 for r in exits if Decimal(r["filled_shares"]) > 0),
        "mean_entry_slippage_vs_planned_vwap": sum(slippages) / len(slippages)
        if slippages
        else None,
        "max_entry_slippage_vs_planned_vwap": max(slippages) if slippages else None,
        "fees_usd": float(sum((Decimal(r["fees_usd"]) for r in rows), Decimal(0))),
    }


def _equity_curve(db: Path) -> list[float]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [float(r[0]) for r in conn.execute("SELECT equity FROM equity_marks ORDER BY ts_ms")]
    finally:
        conn.close()


def build_report(run: ReplayRun, config: AppConfig) -> dict[str, Any]:
    core = run.assembly.core
    db = run.assembly.store.path
    trades: list[TradeLog] = core.trade_log
    pnls = [float(t.trade.pnl_usd) for t in trades]
    fees = [float(t.trade.fees_usd) for t in trades]
    net = sum(pnls)
    preds = core.stats.predictions
    model_pairs = [(p, y) for p, _, y in preds]
    market_pairs = [(m, y) for _, m, y in preds]
    model_brier, market_brier = brier(model_pairs), brier(market_pairs)
    dd_abs, dd_pct = max_drawdown(_equity_curve(db))
    ci = bootstrap_sum_ci(pnls)

    def meta_value(t: TradeLog, name: str) -> float | None:
        return None if t.meta is None else float(getattr(t.meta, name))

    report: dict[str, Any] = {
        "provenance": {
            "session": str(run.session.path),
            "synthetic": run.synthetic,
            "warning": (
                "SYNTHETIC DATA: simulated prices, books and outcomes; results validate "
                "the pipeline only and say nothing about real profitability."
                if run.synthetic
                else "Recorded market data replayed through the paper exchange model."
            ),
            "messages": run.messages,
            "first_ms": run.first_ms,
            "last_ms": run.last_ms,
            "strategy_version": config.strategy.version,
            "policy_hash": core.d.risk.policy_hash,
            "model_version": core.d.fair_value.version,
            "llm": "not called in replay (with-vs-without-Claude comparison NOT AVAILABLE)",
        },
        "pnl": {
            "n_trades": len(trades),
            "net_pnl_usd": net,
            "gross_pnl_usd": net + sum(fees),
            "fees_usd": sum(fees),
            "expectancy_usd": net / len(trades) if trades else None,
            "hit_rate": sum(1 for x in pnls if x > 0) / len(pnls) if pnls else None,
            "net_pnl_ci95": ci,
            "max_drawdown_usd": dd_abs,
            "max_drawdown_pct": dd_pct,
            "final_equity_usd": _f(core.portfolio.equity_usd),
            "open_positions_at_end": len(core.portfolio.positions),
        },
        "execution": _execution_quality(db),
        "calibration": {
            "n": len(preds),
            "brier_model": model_brier,
            "brier_market_implied": market_brier,
            "brier_skill_vs_market": brier_skill(model_brier, market_brier),
            "log_loss_model": log_loss(model_pairs),
            "log_loss_market_implied": log_loss(market_pairs),
            "table": calibration_table(model_pairs),
        },
        "breakdown": {
            "by_edge_bucket": group_by(
                trades,
                lambda t: bucket(meta_value(t, "conservative_edge"), EDGE_BUCKETS),
                lambda t: float(t.trade.pnl_usd),
            ),
            "by_time_to_expiry_at_entry": group_by(
                trades,
                lambda t: bucket(
                    None if t.meta is None else t.meta.time_to_expiry_ms / 1000,
                    TTE_BUCKETS_S,
                    "s",
                ),
                lambda t: float(t.trade.pnl_usd),
            ),
            "by_vol_regime_bps": group_by(
                trades,
                lambda t: bucket(None if t.meta is None else t.meta.sigma_bps, VOL_BUCKETS_BPS),
                lambda t: float(t.trade.pnl_usd),
            ),
            "by_hour_utc": group_by(
                trades,
                lambda t: f"{(t.trade.opened_ms // 3_600_000) % 24:02d}h",
                lambda t: float(t.trade.pnl_usd),
            ),
            "by_exit_kind": group_by(
                trades, lambda t: t.trade.exit_kind, lambda t: float(t.trade.pnl_usd)
            ),
            "by_signal_source": group_by(
                trades,
                lambda t: "unknown" if t.meta is None else t.meta.source,
                lambda t: float(t.trade.pnl_usd),
            ),
        },
        "safety": {
            "final_state": run.assembly.state.state.value,
            "final_state_reason": run.assembly.state.reason,
            "kill_switch_engaged": run.assembly.kill_switch.is_engaged(),
            "execution_violations": list(core.execution.violations),
            "reconciliation_failures": _count(
                db, "SELECT COUNT(*) FROM reconciliation_runs WHERE ok = 0"
            ),
            "incidents": _count(db, "SELECT COUNT(*) FROM incidents"),
        },
        "decisions": core.model_metrics(),
    }
    return report


def _count(db: Path, sql: str) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def render_markdown(report: dict[str, Any]) -> str:
    prov, pnl, cal, ex, safety = (
        report["provenance"],
        report["pnl"],
        report["calibration"],
        report["execution"],
        report["safety"],
    )
    lines = [
        "# Backtest report",
        "",
        f"> **{prov['warning']}**",
        "",
        f"- session: `{prov['session']}` (synthetic={prov['synthetic']}, "
        f"{prov['messages']} messages)",
        f"- strategy `{prov['strategy_version']}`, model `{prov['model_version']}`, "
        f"policy `{prov['policy_hash'][:16]}`",
        f"- Claude: {prov['llm']}",
        "",
        "## PnL",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    lines += [f"| {k} | {_fmt(v)} |" for k, v in pnl.items()]
    lines += ["", "## Execution", "", "| metric | value |", "|---|---|"]
    lines += [f"| {k} | {_fmt(v)} |" for k, v in ex.items()]
    lines += ["", "## Calibration (entries, scored at settlement)", "", "| metric | value |"]
    lines += ["|---|---|"]
    lines += [f"| {k} | {_fmt(v)} |" for k, v in cal.items() if k != "table"]
    lines += ["", "| bin | n | mean predicted | observed |", "|---|---|---|---|"]
    lines += [
        f"| {r['bin']} | {r['n']} | {r['mean_predicted']:.3f} | {r['observed_rate']:.3f} |"
        for r in cal["table"]
    ]
    for name, table in report["breakdown"].items():
        lines += ["", f"## PnL {name.replace('_', ' ')}", "", "| bucket | n | pnl | mean |"]
        lines += ["|---|---|---|---|"]
        lines += [
            f"| {k} | {int(v['n'])} | {v['pnl']:.4f} | {v['mean']:.4f} |" for k, v in table.items()
        ]
    lines += ["", "## Safety", "", "| metric | value |", "|---|---|"]
    lines += [f"| {k} | {_fmt(v)} |" for k, v in safety.items()]
    return "\n".join(lines) + "\n"


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, tuple):
        return "(" + ", ".join(_fmt(x) for x in v) + ")"
    return str(v)


def write_report(report: dict[str, Any], out: Path, name: str = "report") -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md = out / f"{name}.md"
    md.write_text(render_markdown(report), encoding="utf-8")
    return md


async def run_backtest(
    config: AppConfig, session: Path, work_dir: Path
) -> tuple[ReplayRun, dict[str, Any]]:
    run = await run_replay(config, session, work_dir)
    return run, build_report(run, config)


@dataclass(frozen=True)
class Perturbation:
    name: str
    update: dict[str, dict[str, Any]]


PERTURBATIONS: tuple[Perturbation, ...] = (
    Perturbation("baseline", {}),
    Perturbation("latency +300ms", {"paper": {"latency_ms": "+300"}}),
    Perturbation("latency +1000ms", {"paper": {"latency_ms": "+1000"}}),
    Perturbation("fees x1.5", {"paper": {"fee_multiplier": "1.5"}}),
    Perturbation("slippage buffer +1c", {"edge": {"slippage_buffer": "+0.01"}}),
    Perturbation("min edge +2c", {"risk": {"min_conservative_edge": "+0.02"}}),
)


def perturbed(config: AppConfig, p: Perturbation) -> AppConfig:
    data = config.model_dump(mode="json")
    for section, fields in p.update.items():
        for key, value in fields.items():
            if isinstance(value, str) and value.startswith("+"):
                current = Decimal(str(data[section][key]))
                new = current + Decimal(value[1:])
                data[section][key] = int(new) if isinstance(data[section][key], int) else str(new)
            else:
                data[section][key] = value
    return AppConfig.model_validate(data)


async def run_robustness(config: AppConfig, session: Path, work_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, p in enumerate(PERTURBATIONS):
        _, report = await run_backtest(perturbed(config, p), session, work_dir / f"p{i}")
        rows.append(
            {
                "perturbation": p.name,
                "n_trades": report["pnl"]["n_trades"],
                "net_pnl_usd": report["pnl"]["net_pnl_usd"],
                "fees_usd": report["pnl"]["fees_usd"],
                "max_drawdown_usd": report["pnl"]["max_drawdown_usd"],
                "fill_ratio": report["execution"]["entry_fill_ratio"],
                "synthetic": report["provenance"]["synthetic"],
            }
        )
    return rows
