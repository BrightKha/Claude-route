"""Edge Engine: executable, cost-aware, conservative edge from the real book.

For an outcome token with fair value ``f`` and lower bound ``l``:

* walk the asks, only through levels whose *marginal* cost
  (price + fee(price) + slippage buffer) stays at or below
  ``l - min_conservative_edge - expected_exit_cost`` — so every share bought
  clears the minimum edge on its own;
* ``executable_price`` = VWAP of that walk (never a mid or theoretical price);
* ``effective_price`` = VWAP + fee per share + slippage buffer;
* ``expected_exit_cost`` = P(early exit) x (half spread + exit fee at the bid);
* ``conservative_edge`` = l - effective_price - expected_exit_cost;
* ``worst_case_edge`` = same, as if everything filled at the deepest level.

Marginal opportunities are refused (candidate filter), and the Risk Engine
re-checks everything independently.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from decimal import ROUND_FLOOR, Decimal

from polymarket_bot.config.app_config import EdgeConfig
from polymarket_bot.config.risk_policy import RiskPolicy
from polymarket_bot.domain.decisions import TradeCandidate
from polymarket_bot.domain.market import FeeSchedule, OrderBookSnapshot
from polymarket_bot.domain.snapshot import MarketSnapshot
from polymarket_bot.domain.types import Side
from polymarket_bot.features.btc5m import FeatureVector
from polymarket_bot.strategies.btc_5m.fair_value import FairValueEstimate

ZERO = Decimal(0)
SHARE_Q = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class BookWalk:
    shares: Decimal
    notional: Decimal
    fees: Decimal
    worst_price: Decimal | None
    best_price: Decimal | None
    depth_usd_profitable: Decimal

    @property
    def vwap(self) -> Decimal | None:
        return None if self.shares <= 0 else self.notional / self.shares


def walk_asks(
    book: OrderBookSnapshot,
    *,
    budget_usd: Decimal,
    max_marginal_cost: Decimal,
    fees: FeeSchedule,
    slippage_buffer: Decimal,
) -> BookWalk:
    """Buy up to ``budget_usd`` (excluding fees) through profitable ask levels only."""
    shares = notional = fee_total = depth = ZERO
    worst: Decimal | None = None
    best = book.best_ask
    for level in book.asks:
        marginal = level.price + fees.fee_rate_at(level.price) + slippage_buffer
        if marginal > max_marginal_cost:
            break
        depth += level.price * level.size
        remaining = budget_usd - notional
        if remaining <= 0:
            continue
        take = min(level.size, (remaining / level.price).quantize(SHARE_Q, rounding=ROUND_FLOOR))
        if take <= 0:
            continue
        shares += take
        notional += take * level.price
        fee_total += fees.taker_fee(take, level.price)
        worst = level.price
    return BookWalk(shares, notional, fee_total, worst, best, depth)


class EdgeEngine:
    def __init__(self, edge_cfg: EdgeConfig, policy: RiskPolicy) -> None:
        self._cfg = edge_cfg
        self._policy = policy

    def candidates(
        self,
        snapshot: MarketSnapshot,
        fv: FeatureVector,
        estimate: FairValueEstimate,
        *,
        resolution_valid: bool,
    ) -> list[TradeCandidate]:
        """One candidate per outcome (for audit); at most one can pass the filters."""
        out = [
            self._candidate(snapshot, fv, estimate, outcome, resolution_valid)
            for outcome in ("Up", "Down")
        ]
        passing = [c for c in out if c.passes_filters]
        if len(passing) > 1:  # never both sides of the same window
            best = max(passing, key=lambda c: c.conservative_edge)
            out = [
                c if c is best or not c.passes_filters else _reject(c, "other side better")
                for c in out
            ]
        return out

    def _candidate(
        self,
        snap: MarketSnapshot,
        fv: FeatureVector,
        est: FairValueEstimate,
        outcome: str,
        resolution_valid: bool,
    ) -> TradeCandidate:
        p = self._policy
        cfg = self._cfg
        market = snap.market
        quote = snap.quote(outcome)
        fair, lower, upper = est.for_outcome(outcome)
        rejections: list[str] = []
        if not est.ok:
            rejections.extend(f"fair value: {r}" for r in est.reasons)
        if not resolution_valid:
            rejections.append("resolution not validated")
        if not snap.is_fresh:
            rejections.extend(f"stale: {r}" for r in snap.stale_reasons)
        book = quote.book
        budget = min(cfg.target_order_usd, p.max_order_size_usd)
        best_ask = quote.best_ask
        best_bid = quote.best_bid
        half_spread = (quote.spread / 2) if quote.spread is not None else Decimal("0.05")
        exit_fee = (
            market.fee_schedule.fee_rate_at(best_bid) if best_bid is not None else Decimal("0.02")
        )
        exit_cost = cfg.early_exit_probability * (half_spread + exit_fee)
        lower_d = Decimal(str(round(lower, 6)))
        walk = (
            walk_asks(
                book,
                budget_usd=budget,
                # Each marginal share must itself clear the minimum conservative edge.
                max_marginal_cost=lower_d - p.min_conservative_edge - exit_cost,
                fees=market.fee_schedule,
                slippage_buffer=cfg.slippage_buffer,
            )
            if book is not None
            else BookWalk(ZERO, ZERO, ZERO, None, None, ZERO)
        )
        vwap = walk.vwap
        if best_ask is None:
            rejections.append("no ask")
        if vwap is None or walk.worst_price is None:
            rejections.append("no profitable ask levels")
            vwap = best_ask if best_ask is not None else Decimal("0.99")
            worst = vwap
        else:
            worst = walk.worst_price
        shares = walk.shares
        fee_per_share = (
            (walk.fees / shares) if shares > 0 else market.fee_schedule.fee_rate_at(vwap)
        )
        effective = vwap + fee_per_share + cfg.slippage_buffer
        expected_edge = fair - float(effective)
        conservative_edge = lower - float(effective) - float(exit_cost)
        worst_all_in = worst + market.fee_schedule.fee_rate_at(worst) + cfg.slippage_buffer
        # The worst case can never be better than the VWAP-based conservative edge
        # (conservative fee rounding can otherwise make it marginally higher).
        worst_case_edge = min(lower - float(worst_all_in) - float(exit_cost), conservative_edge)
        slippage = (vwap - best_ask) if best_ask is not None else Decimal("1")

        # ---- deterministic candidate filter (the Risk Engine re-checks independently)
        if quote.spread is None or quote.spread > p.max_spread:
            rejections.append(f"spread {quote.spread} > {p.max_spread}")
        if walk.depth_usd_profitable < p.min_liquidity_usd:
            rejections.append(f"liquidity {walk.depth_usd_profitable} < {p.min_liquidity_usd}")
        if slippage > p.max_slippage:
            rejections.append(f"slippage {slippage} > {p.max_slippage}")
        if conservative_edge < float(p.min_conservative_edge):
            rejections.append(
                f"conservative edge {conservative_edge:.4f} < {p.min_conservative_edge}"
            )
        if worst_case_edge < float(p.min_conservative_edge):
            rejections.append(f"worst-case edge {worst_case_edge:.4f} < {p.min_conservative_edge}")
        if upper - lower > float(p.max_uncertainty):
            rejections.append(f"uncertainty {upper - lower:.4f} > {p.max_uncertainty}")
        if snap.time_to_expiry_ms < p.min_time_to_expiry_s * 1000:
            rejections.append("too close to expiry")
        if not (p.min_entry_price <= worst <= p.max_entry_price):
            rejections.append(f"price {worst} outside entry band")
        if shares < market.min_order_size:
            rejections.append(f"size {shares} < min order {market.min_order_size}")
        if est.conflict:
            rejections.append("model conflict (needs review)")
        uncertainty = max(upper - lower, 1e-9)
        confidence = max(0.0, min(1.0, conservative_edge / uncertainty))
        if confidence < cfg.min_confidence:
            rejections.append(f"confidence {confidence:.2f} < {cfg.min_confidence}")
        return TradeCandidate(
            candidate_id=f"cand-{uuid.uuid4().hex[:16]}",
            snapshot_id=snap.snapshot_id,
            condition_id=market.condition_id,
            market_slug=market.slug,
            token_id=quote.token_id,
            outcome=outcome,
            side=Side.BUY,
            fair_probability=fair,
            probability_lower=lower,
            probability_upper=upper,
            executable_price=vwap,
            worst_price=worst,
            effective_price=effective,
            estimated_fee_usd=walk.fees,
            estimated_slippage=slippage,
            expected_edge=expected_edge,
            conservative_edge=conservative_edge,
            worst_case_edge=worst_case_edge,
            liquidity_usd=walk.depth_usd_profitable,
            time_to_expiry_ms=snap.time_to_expiry_ms,
            size_shares=shares,
            notional_usd=walk.notional,
            max_allowed_size_usd=budget,
            signal_version=cfg.signal_version,
            model_version=est.model_version,
            feature_version=fv.feature_version,
            feature_timestamp_ms=fv.t_ms,
            reason=(
                f"{outcome}: fair={fair:.4f} [{lower:.4f},{upper:.4f}] vwap={vwap} "
                f"eff={effective:.4f} cons_edge={conservative_edge:.4f}"
            ),
            confidence=confidence,
            rejections=tuple(rejections),
        )


def _reject(c: TradeCandidate, reason: str) -> TradeCandidate:
    return replace(c, rejections=(*c.rejections, reason))
