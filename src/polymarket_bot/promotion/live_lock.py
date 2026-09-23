"""Multi-layer live lock. LIVE is impossible unless EVERY check passes.

Only this module can mint a :class:`LiveAuthorization`. Claude/MCP have no
path here: environment flags, promotion approvals and compliance attestations
are operator-controlled.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass

from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.config.settings import EnvSettings
from polymarket_bot.lifecycle.state_machine import LiveAuthorization
from polymarket_bot.promotion.gates import PromotionStatus, Stage
from polymarket_bot.security.secrets import LIVE_REQUIRED_SECRET

_MINT_TOKEN = object()
LIVE_CONFIRMATION_PREFIX = "I-ACCEPT-LIVE-RISK"


@dataclass(frozen=True, slots=True)
class LockCheck:
    name: str
    passed: bool
    detail: str


def expected_live_confirmation(policy_hash: str) -> str:
    """The confirmation is bound to the exact risk policy: any change invalidates it."""
    return f"{LIVE_CONFIRMATION_PREFIX}-{policy_hash[:16]}"


def live_adapter_available() -> bool:
    return importlib.util.find_spec("polymarket") is not None


def evaluate_live_lock(
    *,
    env: EnvSettings,
    config: AppConfig,
    policy_hash: str,
    hard_cap_clamps: list[str],
    promotion: PromotionStatus,
    compliance: tuple[bool, str],
    kill_switch_engaged: bool,
    reconciliation_ok: bool,
    market_data_ok: bool,
    credential_names_present: list[str],
    now_ms: int,
) -> tuple[list[LockCheck], LiveAuthorization | None]:
    required_stage = Stage[config.promotion_stage_required_for_live]
    checks = [
        LockCheck("env TRADING_MODE=live", env.trading_mode == "live", env.trading_mode),
        LockCheck("env LIVE_TRADING_ENABLED=true", env.live_trading_enabled is True, ""),
        LockCheck(
            "env LIVE_CONFIRMATION matches current policy hash",
            bool(env.live_confirmation)
            and env.live_confirmation == expected_live_confirmation(policy_hash),
            "bound to policy hash " + policy_hash[:16],
        ),
        LockCheck("config mode=live", config.mode == "live", config.mode),
        LockCheck(
            "risk policy within hard caps (no clamping)",
            not hard_cap_clamps,
            "; ".join(hard_cap_clamps),
        ),
        LockCheck("strategy enabled", config.strategy.enabled, config.strategy.version),
        LockCheck(
            f"promotion approved >= {required_stage.name}",
            promotion.approved_stage >= required_stage,
            f"approved={promotion.approved_stage.name} eligible={promotion.eligible_stage.name}; "
            + "; ".join(promotion.missing_for_next),
        ),
        LockCheck(
            "promotion bound to current policy hash",
            promotion.policy_hash == policy_hash,
            promotion.policy_hash[:16],
        ),
        LockCheck("compliance / jurisdiction", compliance[0], compliance[1]),
        LockCheck("kill switch not engaged", not kill_switch_engaged, ""),
        LockCheck("account reconciled at startup", reconciliation_ok, ""),
        LockCheck("market data healthy", market_data_ok, ""),
        LockCheck(
            "live credentials present (names only)",
            LIVE_REQUIRED_SECRET in credential_names_present,
            ",".join(credential_names_present) or "none",
        ),
        LockCheck("live adapter installed (extra 'live')", live_adapter_available(), ""),
        LockCheck(
            "LLM policy coherent for live",
            config.llm.mode == "off" or not config.llm.allow_trading_without_llm,
            "live requires allow_trading_without_llm=false when LLM review is enabled",
        ),
    ]
    if all(c.passed for c in checks):
        auth = LiveAuthorization(
            policy_hash=policy_hash,
            strategy_version=config.strategy.version,
            issued_ms=now_ms,
            checks=tuple(c.name for c in checks),
            _token=_MINT_TOKEN,
        )
        return checks, auth
    return checks, None
