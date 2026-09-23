"""Exit Engine: explicit, testable exit rules. Never waits for Claude.

Selling a binary share at bid ``b`` yields ``b - fee(b)``; holding it is worth
the (uncertain) probability ``f`` of the outcome. Rules therefore distinguish:

* **value exits** (edge gone / negative / converged): only at a price whose
  net proceeds are not below fair value (minus a tolerance), i.e.
  ``min_price = ceil_tick(f + fee(f) - tolerance)``; take-profit locks a gain
  but never nets less than the conservative lower bound
  (``ceil_tick(lower + fee(lower))``);
* **risk exits** (kill switch, halt, max holding time, invalidation in
  ``always`` mode, time-based): allowed down to a floor
  ``floor_tick(max(lower - risk_exit_discount, min_exit_price))``;
* **holds** when the position cannot be priced safely (stale data, account
  anomaly, final seconds, below min order size) — the maximum loss of a
  binary position is its cost basis, so holding to resolution is bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polymarket_bot.config.app_config import ExitPolicyConfig
from polymarket_bot.domain.decisions import ExitSignal
from polymarket_bot.domain.snapshot import MarketSnapshot
from polymarket_bot.domain.types import BotState
from polymarket_bot.risk.engine import ceil_to_tick, floor_to_tick
from polymarket_bot.strategies.btc_5m.fair_value import FairValueEstimate

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class HeldPosition:
    token_id: str
    condition_id: str
    outcome: str
    shares: Decimal
    avg_entry_price: Decimal  # including entry fees per share
    opened_ms: int
    max_holding_s: int | None = None  # e.g. from a Claude review; can only shorten


@dataclass(frozen=True, slots=True)
class ExitEvaluation:
    action: str  # "EXIT" | "HOLD"
    reasons: tuple[str, ...]
    signal: ExitSignal | None


class ExitEngine:
    def __init__(self, config: ExitPolicyConfig) -> None:
        self._cfg = config

    def evaluate(
        self,
        pos: HeldPosition,
        snap: MarketSnapshot | None,
        estimate: FairValueEstimate | None,
        *,
        bot_state: BotState,
        kill_switch: bool,
        account_anomaly: bool,
        now_ms: int,
    ) -> ExitEvaluation:
        cfg = self._cfg
        if pos.shares <= 0:
            return ExitEvaluation("HOLD", ("no position",), None)
        if account_anomaly:
            return ExitEvaluation("HOLD", ("account anomaly: automated trading suspended",), None)
        if snap is None:
            return ExitEvaluation("HOLD", ("no snapshot: cannot price",), None)
        tau_ms = snap.time_to_expiry_ms
        if tau_ms <= cfg.no_exit_window_s * 1000:
            return ExitEvaluation("HOLD", ("resolution imminent: hold to settlement",), None)
        if pos.shares < snap.market.min_order_size:
            return ExitEvaluation("HOLD", ("below min order size: hold to settlement",), None)
        quote = snap.quote(pos.outcome)
        if (
            not quote.book_valid
            or quote.book_age_ms is None
            or quote.book_age_ms > cfg.exit_stale_data_ms
            or quote.best_bid is None
        ):
            return ExitEvaluation("HOLD", ("stale or missing book: cannot price safely",), None)
        if estimate is None or not estimate.ok:
            why = ", ".join(estimate.reasons) if estimate else "no estimate"
            return ExitEvaluation("HOLD", (f"fair value unavailable ({why})",), None)

        tick = snap.market.tick_size
        fees = snap.market.fee_schedule
        fair, lower, upper = estimate.for_outcome(pos.outcome)
        f, lo, up = (Decimal(str(round(x, 6))) for x in (fair, lower, upper))
        bid = quote.best_bid
        bid_net = bid - fees.fee_rate_at(bid)
        value_floor = max(
            cfg.min_exit_price,
            ceil_to_tick(f + fees.fee_rate_at(f) - cfg.convergence_tolerance, tick),
        )
        risk_floor = max(
            cfg.min_exit_price,
            floor_to_tick(max(lo - cfg.risk_exit_discount, cfg.min_exit_price), tick),
        )
        reasons: list[str] = []
        min_price: Decimal | None = None
        urgency = "normal"

        # --- risk exits (state-driven) --------------------------------------------------
        if kill_switch or bot_state is BotState.KILL_SWITCH:
            if cfg.on_kill_switch == "exit_if_priced":
                reasons.append("kill switch")
                min_price, urgency = risk_floor, "urgent"
            else:
                return ExitEvaluation("HOLD", ("kill switch: policy is hold",), None)
        elif bot_state is BotState.HALTED:
            if cfg.on_halt == "exit_if_priced":
                reasons.append("bot halted")
                min_price, urgency = risk_floor, "urgent"
            else:
                return ExitEvaluation("HOLD", ("halted: policy is hold",), None)

        # --- value exits ----------------------------------------------------------------
        if min_price is None:
            if bid_net > up:
                reasons.append(f"edge negative: bid net {bid_net} > upper {up}")
                min_price = value_floor
            elif bid_net >= f - cfg.convergence_tolerance:
                reasons.append(f"converged: bid net {bid_net} >= fair {f} - tol")
                min_price = value_floor
            elif bid_net - pos.avg_entry_price >= cfg.take_profit_per_share and bid_net >= lo:
                reasons.append(f"take profit: {bid_net - pos.avg_entry_price} per share")
                # Lock the gain, but never net less than the conservative (lower) value.
                min_price = max(cfg.min_exit_price, ceil_to_tick(lo + fees.fee_rate_at(lo), tick))

        # --- time / holding / invalidation risk exits -----------------------------------------
        if min_price is None:
            limits = [s for s in (cfg.max_holding_time_s, pos.max_holding_s) if s is not None]
            if limits and now_ms - pos.opened_ms >= min(limits) * 1000:
                reasons.append(f"max holding time {min(limits)}s reached")
                min_price = risk_floor
            elif up < pos.avg_entry_price - cfg.invalidation_margin:
                if cfg.invalidation_exit_mode == "always":
                    reasons.append(f"signal invalidated: upper {up} < entry {pos.avg_entry_price}")
                    min_price = risk_floor
                else:
                    return ExitEvaluation(
                        "HOLD",
                        ("signal invalidated but bid below fair: holding is higher EV",),
                        None,
                    )
            elif not cfg.hold_to_resolution and bid_net >= lo:
                reasons.append("not holding to resolution by policy")
                min_price = value_floor

        if min_price is None:
            return ExitEvaluation("HOLD", ("no exit rule triggered",), None)
        if bid < min_price:
            return ExitEvaluation(
                "HOLD", (*reasons, f"best bid {bid} below floor {min_price}"), None
            )
        signal = ExitSignal(
            token_id=pos.token_id,
            condition_id=pos.condition_id,
            reasons=tuple(reasons),
            urgency=urgency,
            shares=pos.shares,
            min_price=min_price,
            timestamp_ms=now_ms,
        )
        return ExitEvaluation("EXIT", tuple(reasons), signal)

    def forced_exit(
        self,
        pos: HeldPosition,
        snap: MarketSnapshot | None,
        estimate: FairValueEstimate | None,
        *,
        reason: str,
        now_ms: int,
    ) -> ExitEvaluation:
        """Operator/proposal-requested exit, priced like a risk exit (never a dump).

        Holds when the position cannot be priced safely or the best bid is below the
        conservative floor ``lower - risk_exit_discount``.
        """
        cfg = self._cfg
        if pos.shares <= 0 or snap is None or estimate is None or not estimate.ok:
            return ExitEvaluation("HOLD", (f"{reason}: cannot price safely",), None)
        if snap.time_to_expiry_ms <= cfg.no_exit_window_s * 1000:
            return ExitEvaluation("HOLD", (f"{reason}: resolution imminent",), None)
        if pos.shares < snap.market.min_order_size:
            return ExitEvaluation("HOLD", (f"{reason}: below min order size",), None)
        quote = snap.quote(pos.outcome)
        if (
            not quote.book_valid
            or quote.book_age_ms is None
            or quote.book_age_ms > cfg.exit_stale_data_ms
            or quote.best_bid is None
        ):
            return ExitEvaluation("HOLD", (f"{reason}: stale or missing book",), None)
        _, lower, _ = estimate.for_outcome(pos.outcome)
        lo = Decimal(str(round(lower, 6)))
        tick = snap.market.tick_size
        floor = max(
            cfg.min_exit_price,
            floor_to_tick(max(lo - cfg.risk_exit_discount, cfg.min_exit_price), tick),
        )
        if quote.best_bid < floor:
            return ExitEvaluation(
                "HOLD", (f"{reason}: best bid {quote.best_bid} below floor {floor}",), None
            )
        signal = ExitSignal(
            token_id=pos.token_id,
            condition_id=pos.condition_id,
            reasons=(reason,),
            urgency="normal",
            shares=pos.shares,
            min_price=floor,
            timestamp_ms=now_ms,
        )
        return ExitEvaluation("EXIT", (reason,), signal)
