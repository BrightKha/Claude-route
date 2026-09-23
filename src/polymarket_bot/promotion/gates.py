"""Promotion pipeline: RESEARCH -> BACKTEST -> OUT_OF_SAMPLE -> PAPER -> SMALL_LIVE -> LIVE.

There is no automatic promotion. The *eligible* stage is computed from recorded
evidence; the *approved* stage additionally requires an explicit operator
approval bound to the exact policy hash and strategy version. Evidence produced
from SYNTHETIC data never counts. Neither Claude nor the MCP server can write
evidence or approvals (they have no code path to these functions).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Final


class Stage(IntEnum):
    RESEARCH = 0
    BACKTEST = 1
    OUT_OF_SAMPLE = 2
    PAPER = 3
    SMALL_LIVE = 4
    LIVE = 5


APPROVAL_PHRASE_PREFIX: Final = "APPROVE-STAGE"

# Minimum evidence per stage. Conservative defaults; documented in docs/risk.md.
REQUIREMENTS: Final[Mapping[Stage, Mapping[str, Any]]] = {
    Stage.BACKTEST: {"backtest_min_trades": 200},
    Stage.OUT_OF_SAMPLE: {
        "oos_min_trades": 150,
        "oos_pnl_ci_low_gt": 0.0,
        "oos_brier_skill_gt": 0.0,
    },
    Stage.PAPER: {
        "paper_min_days": 14,
        "paper_min_trades": 150,
        "paper_max_incidents_open": 0,
        "paper_max_reconciliation_failures": 0,
    },
    Stage.SMALL_LIVE: {
        "requires": (
            "tests_passed",
            "security_passed",
            "kill_switch_drill",
            "reconciliation_drill",
        ),
        "approval": True,
    },
    Stage.LIVE: {"small_live_min_days": 30, "small_live_min_trades": 200, "approval": True},
}


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    strategy_version: str
    body: Mapping[str, Any]
    ts_ms: int

    @property
    def synthetic(self) -> bool:
        return bool(self.body.get("synthetic", True))  # unknown provenance => synthetic


@dataclass(frozen=True, slots=True)
class PromotionStatus:
    eligible_stage: Stage
    approved_stage: Stage
    missing_for_next: tuple[str, ...]
    strategy_version: str
    policy_hash: str


def approval_phrase(stage: Stage, policy_hash: str, strategy_version: str) -> str:
    return f"{APPROVAL_PHRASE_PREFIX}-{stage.name}-{strategy_version}-{policy_hash[:16]}"


def _latest(evidence: Iterable[Evidence], kind: str, version: str) -> Evidence | None:
    matching = [
        e for e in evidence if e.kind == kind and e.strategy_version == version and not e.synthetic
    ]
    return max(matching, key=lambda e: e.ts_ms) if matching else None


def _missing_for(stage: Stage, ev: list[Evidence], version: str) -> list[str]:
    req = REQUIREMENTS[stage]
    missing: list[str] = []
    if stage is Stage.BACKTEST:
        bt = _latest(ev, "backtest", version)
        if bt is None or int(bt.body.get("n_trades", 0)) < req["backtest_min_trades"]:
            missing.append(f"non-synthetic backtest with >= {req['backtest_min_trades']} trades")
    elif stage is Stage.OUT_OF_SAMPLE:
        oos = _latest(ev, "oos", version)
        if oos is None:
            missing.append("non-synthetic out-of-sample report")
        else:
            if int(oos.body.get("n_trades", 0)) < req["oos_min_trades"]:
                missing.append(f"OOS trades >= {req['oos_min_trades']}")
            if float(oos.body.get("pnl_ci_low", -1.0)) <= req["oos_pnl_ci_low_gt"]:
                missing.append("OOS net PnL bootstrap CI lower bound > 0")
            if float(oos.body.get("brier_skill", -1.0)) <= req["oos_brier_skill_gt"]:
                missing.append("OOS Brier skill vs market-implied > 0")
    elif stage is Stage.PAPER:
        pp = _latest(ev, "paper_session", version)
        if pp is None:
            missing.append("paper trading evidence")
        else:
            if int(pp.body.get("days", 0)) < req["paper_min_days"]:
                missing.append(f"paper days >= {req['paper_min_days']}")
            if int(pp.body.get("n_trades", 0)) < req["paper_min_trades"]:
                missing.append(f"paper trades >= {req['paper_min_trades']}")
            if int(pp.body.get("open_incidents", 1)) > req["paper_max_incidents_open"]:
                missing.append("no open incidents")
            if (
                int(pp.body.get("reconciliation_failures", 1))
                > req["paper_max_reconciliation_failures"]
            ):
                missing.append("zero reconciliation failures")
    elif stage is Stage.SMALL_LIVE:
        for kind in req["requires"]:
            item = _latest(ev, kind, version)
            if item is None or not bool(item.body.get("passed", False)):
                missing.append(f"{kind} evidence (passed=true)")
    elif stage is Stage.LIVE:
        sl = _latest(ev, "small_live_session", version)
        if sl is None:
            missing.append("SMALL_LIVE session evidence")
        else:
            if int(sl.body.get("days", 0)) < req["small_live_min_days"]:
                missing.append(f"small-live days >= {req['small_live_min_days']}")
            if int(sl.body.get("n_trades", 0)) < req["small_live_min_trades"]:
                missing.append(f"small-live trades >= {req['small_live_min_trades']}")
    return missing


def evaluate_promotion(
    evidence: list[Evidence],
    approvals: list[Mapping[str, Any]],
    *,
    strategy_version: str,
    policy_hash: str,
) -> PromotionStatus:
    eligible = Stage.RESEARCH
    missing_next: list[str] = []
    for stage in list(Stage)[1:]:
        missing = _missing_for(stage, evidence, strategy_version)
        if missing:
            missing_next = missing
            break
        eligible = stage
    approved = Stage.RESEARCH
    for stage in list(Stage)[1:]:
        if stage > eligible:
            break
        if REQUIREMENTS[stage].get("approval"):
            ok = any(
                a.get("stage") == stage.name
                and a.get("policy_hash") == policy_hash
                and a.get("strategy_version") == strategy_version
                and a.get("phrase") == approval_phrase(stage, policy_hash, strategy_version)
                for a in approvals
            )
            if not ok:
                if not missing_next:
                    missing_next = [f"operator approval for {stage.name}"]
                break
        approved = stage
    return PromotionStatus(eligible, approved, tuple(missing_next), strategy_version, policy_hash)
