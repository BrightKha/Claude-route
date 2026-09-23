"""Structured decision objects: candidates, risk decisions, exit decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from polymarket_bot.domain.types import OrderPurpose, Side


@dataclass(frozen=True, slots=True)
class TradeCandidate:
    """A deterministic trade proposal. Never executable by itself."""

    candidate_id: str
    snapshot_id: str
    condition_id: str
    market_slug: str
    token_id: str
    outcome: str
    side: Side
    fair_probability: float
    probability_lower: float
    probability_upper: float
    executable_price: Decimal  # VWAP for the intended size, from the real book
    worst_price: Decimal  # deepest level touched (the limit we would need)
    effective_price: Decimal  # VWAP + fee/share + slippage buffer
    estimated_fee_usd: Decimal
    estimated_slippage: Decimal  # VWAP - best price
    expected_edge: float  # fair - effective
    conservative_edge: float  # lower bound - effective - expected exit cost
    worst_case_edge: float  # conservative edge if *everything* fills at worst_price
    liquidity_usd: Decimal  # notional available up to the limit price
    time_to_expiry_ms: int
    size_shares: Decimal
    notional_usd: Decimal
    max_allowed_size_usd: Decimal
    signal_version: str
    model_version: str
    feature_version: str
    feature_timestamp_ms: int
    reason: str
    confidence: float
    rejections: tuple[str, ...] = ()

    @property
    def uncertainty(self) -> float:
        return self.probability_upper - self.probability_lower

    @property
    def passes_filters(self) -> bool:
        return not self.rejections


@dataclass(frozen=True, slots=True)
class RiskCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Output of the Risk Engine. ``allowed`` requires *every* check to pass."""

    decision_id: str
    allowed: bool
    reasons: tuple[str, ...]
    checks: tuple[RiskCheck, ...]
    purpose: OrderPurpose
    side: Side
    token_id: str
    condition_id: str
    max_size_usd: Decimal  # BUY: max collateral to spend (excluding fees)
    max_shares: Decimal  # SELL: max shares to sell; BUY: informational
    limit_price: Decimal  # BUY: max price; SELL: min price
    risk_version: str
    policy_hash: str
    timestamp_ms: int
    candidate_id: str | None = None
    notes: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class ExitSignal:
    """Why the Exit Engine wants to close (part of) a position."""

    token_id: str
    condition_id: str
    reasons: tuple[str, ...]
    urgency: str  # "normal" | "urgent"
    shares: Decimal
    min_price: Decimal | None  # None => hold (cannot price safely)
    timestamp_ms: int
