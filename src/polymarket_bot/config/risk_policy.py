"""Risk policy: configurable, documented, hashed, clamped by hard caps.

Every field is documented in docs/risk.md. Defaults are conservative starting
points for PAPER trading, not claims of optimality.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from polymarket_bot.risk.hard_caps import HARD_CAPS, SMALL_LIVE_CAPS

RISK_ENGINE_VERSION = "risk-1.0.0"


class RiskPolicy(BaseModel):
    """Operator-configurable limits. Values are *requests*; hard caps win."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- size / exposure -------------------------------------------------------------
    max_position_usd: Decimal = Field(Decimal("20"), gt=0, description="Max cost basis per token")
    max_total_exposure_usd: Decimal = Field(Decimal("60"), gt=0)
    max_order_size_usd: Decimal = Field(Decimal("20"), gt=0)
    max_open_positions: int = Field(2, ge=1)
    max_positions_per_market: int = Field(1, ge=1, description="Across both outcomes")
    min_balance_reserve_usd: Decimal = Field(Decimal("0"), ge=0)

    # --- loss limits (breach => KILL_SWITCH) ------------------------------------------
    max_daily_loss_usd: Decimal = Field(Decimal("30"), gt=0)
    max_daily_loss_pct: Decimal = Field(Decimal("0.10"), gt=0, le=1)
    max_drawdown_pct: Decimal = Field(Decimal("0.20"), gt=0, le=1)
    max_consecutive_losses: int = Field(6, ge=1, description="Reached => entries blocked")

    # --- market quality ------------------------------------------------------------
    max_spread: Decimal = Field(Decimal("0.04"), gt=0)
    max_slippage: Decimal = Field(Decimal("0.02"), ge=0)
    min_liquidity_usd: Decimal = Field(Decimal("25"), ge=0)
    min_entry_price: Decimal = Field(Decimal("0.05"), gt=0, lt=1)
    max_entry_price: Decimal = Field(Decimal("0.95"), gt=0, lt=1)

    # --- signal quality ------------------------------------------------------------
    min_conservative_edge: Decimal = Field(Decimal("0.03"), gt=0)
    max_uncertainty: Decimal = Field(Decimal("0.25"), gt=0, le=1)

    # --- freshness / timing --------------------------------------------------------------
    max_data_age_ms: int = Field(2000, gt=0)
    max_reference_age_ms: int = Field(5000, gt=0)
    max_clock_drift_ms: int = Field(1000, gt=0)
    max_order_age_seconds: int = Field(30, gt=0, description="Resting orders are cancelled after")
    min_time_to_expiry_s: int = Field(45, ge=0)
    min_time_since_start_s: int = Field(5, ge=0, description="Price-to-beat must be verified")
    require_reconciled_within_s: int = Field(120, gt=0)

    # --- rate limits / cooldowns ----------------------------------------------------------
    max_trades_per_minute: int = Field(2, ge=1)
    max_trades_per_hour: int = Field(20, ge=1)
    max_trades_per_day: int = Field(120, ge=1)
    cooldown_same_market_s: int = Field(30, ge=0)
    cooldown_after_loss_s: int = Field(60, ge=0)

    # --- exits ---------------------------------------------------------------------
    allow_exits_when_halted: bool = True
    exits_require_fresh_book: bool = True

    def canonical_json(self) -> str:
        data: dict[str, Any] = self.model_dump(mode="json")
        return json.dumps(data, sort_keys=True, separators=(",", ":"))


def policy_hash(policy: RiskPolicy) -> str:
    """Stable hash binding decisions and live confirmation to an exact policy."""
    payload = f"{RISK_ENGINE_VERSION}|{policy.canonical_json()}".encode()
    return hashlib.sha256(payload).hexdigest()


def clamp_to_hard_caps(
    policy: RiskPolicy, *, small_live: bool = False
) -> tuple[RiskPolicy, list[str]]:
    """Return the effective policy and the list of clamped fields.

    ``small_live=True`` additionally applies the SMALL_LIVE stage caps.
    """
    updates: dict[str, Any] = {}
    notes: list[str] = []
    current = policy.model_dump()
    for name, cap in HARD_CAPS.items():
        if name not in current:
            raise KeyError(f"hard cap refers to unknown policy field {name}")
        value = Decimal(str(current[name]))
        if cap.kind == "max" and value > cap.value:
            updates[name] = _coerce(current[name], cap.value)
            notes.append(f"{name}: {value} clamped to hard cap {cap.value}")
        elif cap.kind == "min" and value < cap.value:
            updates[name] = _coerce(current[name], cap.value)
            notes.append(f"{name}: {value} raised to hard floor {cap.value}")
    if small_live:
        for name, cap_value in SMALL_LIVE_CAPS.items():
            value = Decimal(str(updates.get(name, current[name])))
            if value > cap_value:
                updates[name] = _coerce(current[name], cap_value)
                notes.append(f"{name}: {value} clamped to SMALL_LIVE cap {cap_value}")
    if updates:
        policy = policy.model_copy(update=updates)
    if policy.min_entry_price >= policy.max_entry_price:
        raise ValueError("min_entry_price must be < max_entry_price")
    return policy, notes


def _coerce(original: object, value: Decimal) -> int | Decimal:
    return int(value) if isinstance(original, int) and not isinstance(original, bool) else value
