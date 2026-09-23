"""Claude review I/O contracts.

* :class:`ReviewContext` is the ONLY data sent to Claude: an explicit whitelist
  of market/model numbers. No wallet, balance, key, order id or config value.
* :class:`LLMReview` is the ONLY shape accepted back. Anything else (refusal,
  truncated or schema-invalid output) is treated as a rejection by the caller.
* :func:`tighten_candidate` applies a review deterministically: a probability
  override can only *lower* the candidate's probability band (entries are
  BUY-only, so this can only reduce the edge); an upward override is ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from polymarket_bot.domain.decisions import TradeCandidate
from polymarket_bot.domain.snapshot import MarketSnapshot
from polymarket_bot.llm.budget import TokenUsage

REVIEW_SCHEMA_VERSION: Final = "llm-review-1"
PROMPT_VERSION: Final = "btc5m-review-1"
CONTEXT_SCHEMA_VERSION: Final = "review-context-1"
MAX_HOLDING_TIME_S: Final = 300  # a BTC 5m window never lasts longer
PROBABILITY_FLOOR: Final = 0.001


class LLMReview(BaseModel):
    """Structured output requested from Claude (validated client-side, fail closed)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["APPROVE", "REJECT", "NO_OP"]
    fair_probability_override: float | None = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str = Field(max_length=2000)
    key_risks: list[str] = Field(max_length=10)
    invalidators: list[str] = Field(max_length=10)
    recommended_exit_conditions: list[str] = Field(max_length=10)
    max_holding_time_seconds: int | None = Field(ge=0, le=MAX_HOLDING_TIME_S)


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


_STR_LIST: Final[dict[str, Any]] = {"type": "array", "items": {"type": "string"}}

# Hand-written (not generated) so the wire schema stays within the structured-output
# subset: every property required, no additional properties, nullability via anyOf.
# Numeric ranges and lengths are enforced by LLMReview after parsing.
REVIEW_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["APPROVE", "REJECT", "NO_OP"]},
        "fair_probability_override": _nullable({"type": "number"}),
        "confidence": {"type": "number"},
        "thesis": {"type": "string"},
        "key_risks": _STR_LIST,
        "invalidators": _STR_LIST,
        "recommended_exit_conditions": _STR_LIST,
        "max_holding_time_seconds": _nullable({"type": "integer"}),
    },
    "required": [
        "action",
        "fair_probability_override",
        "confidence",
        "thesis",
        "key_risks",
        "invalidators",
        "recommended_exit_conditions",
        "max_holding_time_seconds",
    ],
    "additionalProperties": False,
}


class ReviewContext(BaseModel):
    """Whitelisted, numbers-first context for one candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["review-context-1"] = CONTEXT_SCHEMA_VERSION
    market_slug: str = Field(max_length=120)
    question: str = Field(max_length=300)
    resolution_rule_id: str = Field(max_length=64)
    seconds_to_expiry: float
    seconds_since_start: float
    outcome: str = Field(max_length=16)
    side: Literal["BUY"]
    reference_spot: float | None
    reference_twap: float | None
    price_to_beat: float | None
    price_to_beat_verified: bool
    secondary_spot: float | None
    source_dispersion_bps: float | None
    volatility_bps_per_sqrt_s: float | None
    fair_probability: float
    probability_lower: float
    probability_upper: float
    best_bid: float | None
    best_ask: float | None
    executable_price: float
    effective_price_incl_fees: float
    conservative_edge: float
    worst_case_edge: float
    liquidity_usd: float
    order_notional_usd: float
    model_version: str = Field(max_length=64)
    feature_version: str = Field(max_length=64)
    data_quality_flags: list[str] = Field(max_length=10)


def build_review_context(
    candidate: TradeCandidate,
    snapshot: MarketSnapshot,
    *,
    volatility_bps_per_sqrt_s: float | None,
) -> ReviewContext:
    ref = snapshot.reference
    quote = snapshot.quote(candidate.outcome)
    f = _opt_float
    return ReviewContext(
        market_slug=candidate.market_slug,
        question=snapshot.market.question[:300],
        resolution_rule_id=snapshot.market.rule_id,
        seconds_to_expiry=snapshot.time_to_expiry_ms / 1000,
        seconds_since_start=snapshot.time_since_start_ms / 1000,
        outcome=candidate.outcome,
        side="BUY",
        reference_spot=f(ref.spot),
        reference_twap=f(ref.twap),
        price_to_beat=f(ref.price_to_beat),
        price_to_beat_verified=ref.price_to_beat_verified,
        secondary_spot=f(ref.secondary_spot),
        source_dispersion_bps=ref.dispersion_bps,
        volatility_bps_per_sqrt_s=volatility_bps_per_sqrt_s,
        fair_probability=candidate.fair_probability,
        probability_lower=candidate.probability_lower,
        probability_upper=candidate.probability_upper,
        best_bid=f(quote.best_bid),
        best_ask=f(quote.best_ask),
        executable_price=float(candidate.executable_price),
        effective_price_incl_fees=float(candidate.effective_price),
        conservative_edge=candidate.conservative_edge,
        worst_case_edge=candidate.worst_case_edge,
        liquidity_usd=float(candidate.liquidity_usd),
        order_notional_usd=float(candidate.notional_usd),
        model_version=candidate.model_version,
        feature_version=candidate.feature_version,
        data_quality_flags=list(snapshot.stale_reasons)[:10],
    )


def _opt_float(x: Decimal | float | None) -> float | None:
    return None if x is None else float(x)


def tighten_candidate(candidate: TradeCandidate, review: LLMReview) -> TradeCandidate:
    """Shift the probability band *down* by how far Claude's override is below fair.

    ``conservative_edge`` and ``worst_case_edge`` drop by the same amount, so the
    Risk Engine re-checks the tightened candidate against unchanged thresholds.
    """
    override = review.fair_probability_override
    if override is None or override >= candidate.fair_probability:
        return candidate
    delta = candidate.fair_probability - override
    return replace(
        candidate,
        fair_probability=max(PROBABILITY_FLOOR, candidate.fair_probability - delta),
        probability_lower=max(PROBABILITY_FLOOR, candidate.probability_lower - delta),
        probability_upper=max(PROBABILITY_FLOOR, candidate.probability_upper - delta),
        expected_edge=candidate.expected_edge - delta,
        conservative_edge=candidate.conservative_edge - delta,
        worst_case_edge=candidate.worst_case_edge - delta,
        reason=f"{candidate.reason}; tightened by llm override {override:.4f}",
    )


Outcome = Literal[
    "ok", "refusal", "invalid_output", "truncated", "timeout", "error", "unavailable", "blocked"
]


@dataclass(frozen=True, slots=True)
class ClaudeCallResult:
    outcome: Outcome
    review: LLMReview | None
    cost_usd: Decimal
    usage: TokenUsage | None
    model: str | None
    latency_ms: int
    fallback_used: bool
    detail: str
    prompt_version: str = PROMPT_VERSION
