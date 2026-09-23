"""Command line interface.

    python -m polymarket_bot.app --mode paper|replay|live [--config ...]
    python -m polymarket_bot.app <command> [...]

LIVE is locked: ``--mode live`` evaluates every live-lock precondition and
refuses to start unless all pass (it never changes a flag, a limit or a
promotion stage by itself).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from polymarket_bot.audit.audit_log import AuditLog, verify_audit_log
from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.config.loader import load_config
from polymarket_bot.config.risk_policy import clamp_to_hard_caps, policy_hash
from polymarket_bot.config.settings import load_env_settings
from polymarket_bot.domain.clock import SystemClock
from polymarket_bot.domain.types import OrderStatus
from polymarket_bot.lifecycle.kill_switch import KillSwitch, KillSwitchError
from polymarket_bot.lifecycle.state_machine import BotStateMachine
from polymarket_bot.monitoring.logging_setup import setup_logging
from polymarket_bot.promotion.gates import Evidence, Stage, approval_phrase, evaluate_promotion
from polymarket_bot.promotion.live_lock import evaluate_live_lock
from polymarket_bot.security.compliance import compliance_gate
from polymarket_bot.security.redaction import install_excepthook
from polymarket_bot.security.secrets import secret_env_vars_present
from polymarket_bot.storage.sqlite_store import StateStore

log = logging.getLogger("polymarket_bot.cli")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_LOCKED = 2


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _config(args: argparse.Namespace) -> AppConfig:
    return load_config(args.config)


def _data_dir(args: argparse.Namespace, config: AppConfig) -> Path:
    if getattr(args, "data_dir", None):
        return Path(args.data_dir)
    env_dir = load_env_settings().bot_data_dir
    return Path(env_dir) if env_dir else config.data_dir


def _policy(config: AppConfig, *, small_live: bool = False) -> tuple[str, list[str]]:
    policy, notes = clamp_to_hard_caps(config.risk, small_live=small_live)
    return policy_hash(policy), notes


# ---------------------------------------------------------------------- research commands
def cmd_synth(args: argparse.Namespace) -> int:
    from polymarket_bot.research.synthetic import SynthParams, generate_session  # noqa: PLC0415

    out = Path(args.out)
    params = SynthParams(
        windows=args.windows,
        seed=args.seed,
        mm_noise=args.mm_noise,
        disconnect_prob_per_window=args.disconnect_prob,
    )
    path = generate_session(out.parent, params, session_id=out.name)
    print(f"SYNTHETIC session written to {path} (synthetic=true; not market evidence)")
    return EXIT_OK


def _work_dir(args: argparse.Namespace, config: AppConfig, kind: str) -> Path:
    if getattr(args, "out", None):
        return Path(args.out)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
    return _data_dir(args, config) / kind / stamp


def cmd_replay(args: argparse.Namespace) -> int:
    from polymarket_bot.app.replay_engine import run_replay  # noqa: PLC0415

    config = _config(args)
    work = _work_dir(args, config, "replay")
    run = asyncio.run(run_replay(config, Path(args.input), work))
    core = run.assembly.core
    _print(
        {
            "synthetic": run.synthetic,
            "messages": run.messages,
            "steps": core.stats.steps,
            "final_state": run.assembly.state.state.value,
            "trades": len(core.trade_log),
            "realized_pnl_usd": core.portfolio.realized_pnl_usd,
            "equity_usd": core.portfolio.equity_usd,
            "violations": core.execution.violations,
            "work_dir": str(work),
        }
    )
    return EXIT_OK


def cmd_backtest(args: argparse.Namespace) -> int:
    from polymarket_bot.research.backtest import (  # noqa: PLC0415
        run_backtest,
        run_robustness,
        write_report,
    )

    config = _config(args)
    work = _work_dir(args, config, "backtest")
    report_dir = Path(args.report)
    _, report = asyncio.run(run_backtest(config, Path(args.input), work / "base"))
    md = write_report(report, report_dir)
    print(f"report: {md}")
    if args.robustness:
        rows = asyncio.run(run_robustness(config, Path(args.input), work / "robustness"))
        (report_dir / "robustness.json").write_text(json.dumps(rows, indent=2, default=str))
        lines = ["# Robustness", "", "| perturbation | trades | net pnl | fees | max dd | fill |"]
        lines.append("|---|---|---|---|---|---|")
        lines += [
            f"| {r['perturbation']} | {r['n_trades']} | {r['net_pnl_usd']:.4f} | "
            f"{r['fees_usd']:.4f} | {r['max_drawdown_usd']:.4f} | {r['fill_ratio']} |"
            for r in rows
        ]
        (report_dir / "robustness.md").write_text("\n".join(lines) + "\n")
        print(f"robustness: {report_dir / 'robustness.md'}")
    if args.record_evidence:
        _record_evidence(args, config, "backtest", report)
    return EXIT_OK


def cmd_walk_forward(args: argparse.Namespace) -> int:
    from polymarket_bot.research.walk_forward import run_study  # noqa: PLC0415

    study = run_study(_config(args), Path(args.input), Path(args.report))
    summary = {k: v for k, v in study.items() if k != "folds"}
    _print(summary | {"folds": len(study.get("folds", []))})
    return EXIT_OK


def _record_evidence(
    args: argparse.Namespace, config: AppConfig, kind: str, report: dict[str, Any]
) -> None:
    store = StateStore(_data_dir(args, config) / "state.sqlite")
    body = {
        "kind": kind,
        "strategy_version": config.strategy.version,
        "synthetic": report["provenance"]["synthetic"],
        "n_trades": report["pnl"]["n_trades"],
        "net_pnl_usd": report["pnl"]["net_pnl_usd"],
        "session": report["provenance"]["session"],
    }
    store.insert_promotion_event(
        ts_ms=int(time.time() * 1000), stage="-", action="evidence", actor="cli", body=body
    )
    print(f"evidence recorded (synthetic={body['synthetic']}; synthetic evidence never counts)")


# ---------------------------------------------------------------------- runtime commands
def cmd_paper(args: argparse.Namespace) -> int:
    from polymarket_bot.app.runner import run_paper  # noqa: PLC0415

    config = _config(args)
    if config.mode not in ("paper", "disabled"):
        print(f"config mode is {config.mode!r}; paper runner refuses", file=sys.stderr)
        return EXIT_LOCKED
    asyncio.run(run_paper(config, _data_dir(args, config), trade=True))
    return EXIT_OK


def cmd_record(args: argparse.Namespace) -> int:
    from polymarket_bot.app.runner import run_paper  # noqa: PLC0415

    config = _config(args)
    asyncio.run(run_paper(config, _data_dir(args, config), trade=False))
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    config = _config(args)
    path = _data_dir(args, config) / "state.sqlite"
    if not path.exists():
        _print({"state": "no state database", "path": str(path)})
        return EXIT_OK
    store = StateStore(path, read_only=True)
    engaged, reason = store.kill_switch_state()
    audit_ok, audit_n, audit_detail = verify_audit_log(path.parent / "audit.jsonl")
    _print(
        {
            "bot_state": store.load_bot_state(),
            "kill_switch": {"engaged": engaged, "reason": reason},
            "runtime": store.read_status("runtime"),
            "portfolio": store.read_status("portfolio"),
            "pnl": store.read_status("pnl"),
            "health": store.read_status("health"),
            "last_reconciliation": store.last_reconciliation(),
            "open_orders": len(store.load_orders(only_open=True)),
            "recent_incidents": store.recent_incidents(5),
            "audit_log": {"valid": audit_ok, "records": audit_n, "detail": audit_detail},
        }
    )
    return EXIT_OK


def _kill_switch(args: argparse.Namespace, config: AppConfig) -> KillSwitch:
    data_dir = _data_dir(args, config)
    clock = SystemClock()
    store = StateStore(data_dir / "state.sqlite")
    return KillSwitch(
        data_dir, store, BotStateMachine(clock), AuditLog(data_dir / "audit.jsonl", clock), clock
    )


def cmd_kill_switch(args: argparse.Namespace) -> int:
    config = _config(args)
    ks = _kill_switch(args, config)
    if args.action == "engage":
        ks.engage(args.reason, source="operator-cli")
        print(f"KILL SWITCH ENGAGED: {args.reason} (sentinel {ks.sentinel_path})")
        return EXIT_OK
    if args.action == "status":
        _print({"engaged": ks.is_engaged(), "reason": ks.reason()})
        return EXIT_OK
    try:
        ks.reset(operator=args.operator, confirmation=args.confirm, investigation_note=args.note)
    except KillSwitchError as exc:
        print(f"reset refused: {exc}", file=sys.stderr)
        return EXIT_FAIL
    print("kill switch reset; bot returns to DISABLED (restart required)")
    return EXIT_OK


def _promotion(args: argparse.Namespace, config: AppConfig, phash: str) -> Any:
    path = _data_dir(args, config) / "state.sqlite"
    events = StateStore(path, read_only=True).promotion_events() if path.exists() else []
    evidence = [
        Evidence(
            kind=str(e["body"].get("kind")),
            strategy_version=str(e["body"].get("strategy_version")),
            body=e["body"],
            ts_ms=int(e["ts_ms"]),
        )
        for e in events
        if e["action"] == "evidence"
    ]
    approvals = [e["body"] for e in events if e["action"] == "approve"]
    return evaluate_promotion(
        evidence, approvals, strategy_version=config.strategy.version, policy_hash=phash
    )


def cmd_promotion(args: argparse.Namespace) -> int:
    config = _config(args)
    phash, _ = _policy(config)
    if args.action == "status":
        status = _promotion(args, config, phash)
        _print(
            {
                "eligible_stage": status.eligible_stage.name,
                "approved_stage": status.approved_stage.name,
                "missing_for_next": status.missing_for_next,
                "strategy_version": status.strategy_version,
                "policy_hash": status.policy_hash,
            }
        )
        return EXIT_OK
    stage = Stage[args.stage]
    expected = approval_phrase(stage, phash, config.strategy.version)
    if args.phrase != expected:
        print(f"approval refused: phrase must be exactly {expected}", file=sys.stderr)
        return EXIT_FAIL
    store = StateStore(_data_dir(args, config) / "state.sqlite")
    store.insert_promotion_event(
        ts_ms=int(time.time() * 1000),
        stage=stage.name,
        action="approve",
        actor=args.operator,
        body={
            "stage": stage.name,
            "policy_hash": phash,
            "strategy_version": config.strategy.version,
            "phrase": args.phrase,
            "operator": args.operator,
        },
    )
    print(f"approval recorded for {stage.name} (effective only if evidence makes it eligible)")
    return EXIT_OK


async def _live_checks(args: argparse.Namespace, config: AppConfig) -> tuple[list[Any], Any]:
    env = load_env_settings()
    small_live = config.promotion_stage_required_for_live == "SMALL_LIVE"
    # Any clamping (hard caps, and SMALL_LIVE caps for that stage) blocks live.
    phash, notes = _policy(config, small_live=small_live)
    promotion = _promotion(args, config, phash)
    compliance = await compliance_gate(config.compliance, env.operator_jurisdiction)
    data_dir = _data_dir(args, config)
    path = data_dir / "state.sqlite"
    engaged = (data_dir / "KILL_SWITCH").exists()
    if path.exists():
        engaged = engaged or StateStore(path, read_only=True).kill_switch_state()[0]
    return evaluate_live_lock(
        env=env,
        config=config,
        policy_hash=phash,
        hard_cap_clamps=notes,
        promotion=promotion,
        compliance=compliance,
        kill_switch_engaged=engaged,
        reconciliation_ok=False,  # proven only by the live runner at startup
        market_data_ok=False,  # proven only by the live runner at startup
        credential_names_present=secret_env_vars_present(),
        now_ms=int(time.time() * 1000),
    )


def cmd_live_readiness(args: argparse.Namespace) -> int:
    config = _config(args)
    checks, _ = asyncio.run(_live_checks(args, config))
    for c in checks:
        mark = "PASS" if c.passed else "FAIL"
        print(f"[{mark}] {c.name}" + (f" — {c.detail}" if c.detail else ""))
    print(
        "\nLIVE stays locked. Reconciliation and market-data checks can only pass inside the "
        "live runner at startup. This command never enables anything."
    )
    return EXIT_OK if all(c.passed for c in checks) else EXIT_LOCKED


def cmd_live(args: argparse.Namespace) -> int:
    config = _config(args)
    checks, _ = asyncio.run(_live_checks(args, config))
    failed = [c for c in checks if not c.passed and c.name not in _RUNTIME_ONLY_CHECKS]
    if failed:
        print("LIVE LOCKED. Failing preconditions:", file=sys.stderr)
        for c in failed:
            print(f"  - {c.name}: {c.detail}", file=sys.stderr)
        return EXIT_LOCKED
    from polymarket_bot.app.runner import run_live  # noqa: PLC0415

    return asyncio.run(run_live(config, _data_dir(args, config)))


def cmd_orders(args: argparse.Namespace) -> int:
    """Operator maintenance for orders left open/UNKNOWN (run with the bot stopped)."""
    config = _config(args)
    data_dir = _data_dir(args, config)
    store = StateStore(data_dir / "state.sqlite", read_only=args.action == "list")
    open_orders = store.load_orders(only_open=True)
    if args.action == "list":
        _print(
            [
                {
                    "intent_id": r.intent.intent_id,
                    "status": r.status.value,
                    "market_slug": r.intent.market_slug,
                    "side": r.intent.side.value,
                    "exchange_order_id": r.exchange_order_id,
                    "created_ms": r.intent.created_ms,
                    "last_error": r.last_error,
                }
                for r in open_orders
            ]
        )
        return EXIT_OK
    target = next((r for r in open_orders if r.intent.intent_id == args.intent), None)
    if target is None or not args.operator.strip() or len(args.note.strip()) < 10:
        print("refused: need an open intent id, --operator and a --note (>= 10 chars)",
              file=sys.stderr)  # fmt: skip
        return EXIT_FAIL
    now = int(time.time() * 1000)
    resolved = replace(
        target,
        status=OrderStatus.CANCELLED,
        updated_ms=now,
        last_error=f"operator {args.operator}: verified no fill on venue: {args.note}",
    )
    store.update_order(resolved)
    AuditLog(data_dir / "audit.jsonl", SystemClock()).append(
        "operator_order_resolution",
        {"intent_id": args.intent, "operator": args.operator, "note": args.note},
    )
    print(f"{args.intent} marked CANCELLED (no fill). Reconciliation will verify on restart.")
    return EXIT_OK


_RUNTIME_ONLY_CHECKS = frozenset({"account reconciled at startup", "market data healthy"})


def cmd_mcp(args: argparse.Namespace) -> int:
    from polymarket_bot.mcp_server.server import McpStartupError, run_stdio  # noqa: PLC0415

    config = _config(args)
    try:
        run_stdio(_data_dir(args, config), config.mcp)
    except McpStartupError as exc:
        print(f"MCP server refused to start: {exc}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="polymarket-bot", description=__doc__)
    p.add_argument("--config", default="configs/paper.yaml")
    p.add_argument("--data-dir", default=None)
    p.add_argument("--log-level", default=None)
    p.add_argument("--mode", choices=("paper", "replay", "live"), default=None)
    p.add_argument("--input", help="session for --mode replay")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("synth", help="generate a SYNTHETIC session")
    s.add_argument("--out", required=True)
    s.add_argument("--windows", type=int, default=288)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--mm-noise", type=float, default=0.35)
    s.add_argument("--disconnect-prob", type=float, default=0.0)
    s.set_defaults(func=cmd_synth)

    s = sub.add_parser("replay", help="replay a session through the paper exchange")
    s.add_argument("--input", required=True)
    s.add_argument("--out", default=None)
    s.set_defaults(func=cmd_replay)

    s = sub.add_parser("backtest", help="replay + metrics report")
    s.add_argument("--input", required=True)
    s.add_argument("--report", required=True)
    s.add_argument("--out", default=None)
    s.add_argument("--robustness", action="store_true")
    s.add_argument("--record-evidence", action="store_true")
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("walk-forward", help="walk-forward calibration study")
    s.add_argument("--input", required=True)
    s.add_argument("--report", required=True)
    s.set_defaults(func=cmd_walk_forward)

    sub.add_parser("paper", help="paper trading on live public data").set_defaults(func=cmd_paper)
    sub.add_parser("record", help="record live public data only").set_defaults(func=cmd_record)
    sub.add_parser("status", help="read-only status").set_defaults(func=cmd_status)
    sub.add_parser("live-readiness", help="live-lock checklist").set_defaults(
        func=cmd_live_readiness
    )
    sub.add_parser("mcp-server", help="restricted MCP server (stdio)").set_defaults(func=cmd_mcp)

    s = sub.add_parser("kill-switch", help="engage / inspect / reset the kill switch")
    s.add_argument("action", choices=("engage", "status", "reset"))
    s.add_argument("--reason", default="operator request")
    s.add_argument("--operator", default="")
    s.add_argument("--note", default="")
    s.add_argument("--confirm", default="")
    s.set_defaults(func=cmd_kill_switch)

    s = sub.add_parser("orders", help="list open/UNKNOWN orders; resolve one as no-fill")
    s.add_argument("action", choices=("list", "resolve-no-fill"))
    s.add_argument("--intent", default="")
    s.add_argument("--operator", default="")
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_orders)

    s = sub.add_parser("promotion", help="promotion status / operator approval")
    s.add_argument("action", choices=("status", "approve"))
    s.add_argument("--stage", choices=[st.name for st in Stage][1:], default="BACKTEST")
    s.add_argument("--phrase", default="")
    s.add_argument("--operator", default="")
    s.set_defaults(func=cmd_promotion)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = args.log_level or os.environ.get("LOG_LEVEL", "INFO").upper()
    setup_logging(level)
    install_excepthook()
    if args.command is None and args.mode is not None:
        if args.mode == "paper":
            return cmd_paper(args)
        if args.mode == "live":
            return cmd_live(args)
        if not args.input:
            parser.error("--mode replay requires --input")
        args.out = None
        return cmd_replay(args)
    if args.command is None:
        parser.print_help()
        return EXIT_FAIL
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
