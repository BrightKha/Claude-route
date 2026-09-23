"""Absolute safety ceilings, independent of any configuration file.

These values can only change through a reviewed code change. No config file,
environment variable, operator command, LLM output or MCP call can raise them.
If a configured value exceeds a cap, the Risk Engine silently clamps it for
paper/replay and *refuses to start live* (a policy outside the caps must be a
deliberate code change, never an accident).

The numbers are intentionally small: they bound the damage of a bug or a bad
configuration during the first live stages. They are NOT claimed to be optimal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class Cap:
    """``kind='max'``: configured value is clamped down; ``'min'``: clamped up."""

    kind: str
    value: Decimal
    why: str


# Stage-independent absolute caps (apply to every mode, including LIVE).
HARD_CAPS: Final = MappingProxyType(
    {
        "max_position_usd": Cap("max", Decimal("100"), "worst-case loss of one binary position"),
        "max_total_exposure_usd": Cap("max", Decimal("300"), "sum of open cost bases"),
        "max_order_size_usd": Cap("max", Decimal("100"), "single order notional"),
        "max_daily_loss_usd": Cap("max", Decimal("100"), "daily realized+unrealized loss"),
        "max_daily_loss_pct": Cap("max", Decimal("0.20"), "fraction of start-of-day equity"),
        "max_drawdown_pct": Cap("max", Decimal("0.30"), "fraction of peak equity"),
        "max_open_positions": Cap("max", Decimal("5"), "simultaneous positions"),
        "max_trades_per_minute": Cap("max", Decimal("4"), "order submissions per minute"),
        "max_trades_per_hour": Cap("max", Decimal("40"), "order submissions per hour"),
        "max_trades_per_day": Cap("max", Decimal("300"), "order submissions per day"),
        "max_spread": Cap("max", Decimal("0.10"), "quoted spread in price units"),
        "max_slippage": Cap("max", Decimal("0.05"), "VWAP minus best price"),
        "max_data_age_ms": Cap("max", Decimal("5000"), "order book staleness"),
        "max_reference_age_ms": Cap("max", Decimal("10000"), "resolution-source price staleness"),
        "max_uncertainty": Cap("max", Decimal("0.40"), "probability interval width"),
        "max_entry_price": Cap("max", Decimal("0.97"), "avoid near-certain outcomes"),
        "max_clock_drift_ms": Cap("max", Decimal("2000"), "local vs exchange clock"),
        "min_conservative_edge": Cap("min", Decimal("0.01"), "edge after all costs"),
        "min_liquidity_usd": Cap("min", Decimal("5"), "depth up to the limit price"),
        "min_time_to_expiry_s": Cap("min", Decimal("20"), "no entries in the final seconds"),
        "min_entry_price": Cap("min", Decimal("0.03"), "avoid lottery tickets"),
        "cooldown_same_market_s": Cap("min", Decimal("5"), "duplicate-order protection"),
    }
)

# Tighter caps while the promotion stage is SMALL_LIVE (first real-money stage).
SMALL_LIVE_CAPS: Final = MappingProxyType(
    {
        "max_position_usd": Decimal("10"),
        "max_total_exposure_usd": Decimal("30"),
        "max_order_size_usd": Decimal("10"),
        "max_daily_loss_usd": Decimal("20"),
        "max_open_positions": Decimal("2"),
        "max_trades_per_hour": Decimal("12"),
    }
)
