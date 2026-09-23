"""The Risk Engine. Deterministic, pure, independent of any LLM.

Every order — entry or exit — must obtain an ``allowed`` :class:`RiskDecision`.
A decision is allowed only if *all* checks pass. The final size and price
limits are computed here, never taken from Claude.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from polymarket_bot.config.risk_policy import RISK_ENGINE_VERSION, RiskPolicy
from polymarket_bot.domain.clock import Clock
from polymarket_bot.domain.decisions import ExitSignal, RiskCheck, RiskDecision, TradeCandidate
from polymarket_bot.domain.snapshot import MarketSnapshot
from polymarket_bot.domain.types import BotState, OrderPurpose, Side, TradingMode

ZERO = Decimal(0)
ONE = Decimal(1)
MIN_ORDER_NOTIONAL_USD = Decimal("1")  # Polymarket rejects dust orders; stay well above


@dataclass(frozen=True, slots=True)
class PositionView:
    token_id: str
    condition_id: str
    shares: Decimal
    cost_basis_usd: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioView:
    cash_usd: Decimal
    equity_usd: Decimal
    start_of_day_equity_usd: Decimal
    peak_equity_usd: Decimal
    positions: dict[str, PositionView]
    pending_buy_usd: Decimal
    pending_sell_shares: dict[str, Decimal]
    consecutive_losses: int
    last_loss_ms: int | None

    @property
    def daily_pnl_usd(self) -> Decimal:
        return self.equity_usd - self.start_of_day_equity_usd

    @property
    def exposure_usd(self) -> Decimal:
        return sum((p.cost_basis_usd for p in self.positions.values()), ZERO) + self.pending_buy_usd


@dataclass(frozen=True, slots=True)
class HealthView:
    market_stream_ok: bool
    reference_stream_ok: bool
    clock_drift_ms: int | None
    last_reconciliation_ms: int | None
    last_reconciliation_ok: bool
    watchdog_ok: bool
    unknown_orders: int
    in_flight_orders: int
    in_flight_tokens: frozenset[str]


@dataclass(frozen=True, slots=True)
class RateView:
    submissions_last_minute: int
    submissions_last_hour: int
    submissions_last_day: int
    exits_last_minute: int
    last_submit_ms_by_market: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EntryContext:
    mode: TradingMode
    bot_state: BotState
    kill_switch_engaged: bool
    resolution_valid: bool
    resolution_detail: str
    portfolio: PortfolioView
    health: HealthView
    rates: RateView
    max_in_flight_orders: int
    llm_size_multiplier: Decimal = ONE


@dataclass(frozen=True, slots=True)
class ExitContext:
    mode: TradingMode
    bot_state: BotState
    kill_switch_engaged: bool
    portfolio: PortfolioView
    health: HealthView
    rates: RateView
    exit_allowed_in_kill_switch: bool


class _Checks:
    def __init__(self) -> None:
        self.items: list[RiskCheck] = []

    def add(self, name: str, passed: bool, detail: str = "") -> bool:
        self.items.append(RiskCheck(name, bool(passed), detail))
        return bool(passed)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(
            f"{c.name}: {c.detail}" if c.detail else c.name for c in self.items if not c.passed
        )


def floor_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def ceil_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return -floor_to_tick(-price, tick)


class RiskEngine:
    def __init__(self, policy: RiskPolicy, policy_hash: str, clock: Clock) -> None:
        self.policy = policy
        self.policy_hash = policy_hash
        self._clock = clock

    # ------------------------------------------------------------------ helpers
    def loss_limit_breaches(self, portfolio: PortfolioView) -> list[str]:
        """Breaches that must trip the kill switch."""
        p = self.policy
        breaches: list[str] = []
        daily = portfolio.daily_pnl_usd
        if daily <= -p.max_daily_loss_usd:
            breaches.append(f"daily loss {daily} <= -{p.max_daily_loss_usd}")
        if portfolio.start_of_day_equity_usd > 0:
            pct = -daily / portfolio.start_of_day_equity_usd
            if pct >= p.max_daily_loss_pct:
                breaches.append(f"daily loss {pct:.4f} >= {p.max_daily_loss_pct} of equity")
        if portfolio.peak_equity_usd > 0:
            dd = (portfolio.peak_equity_usd - portfolio.equity_usd) / portfolio.peak_equity_usd
            if dd >= p.max_drawdown_pct:
                breaches.append(f"drawdown {dd:.4f} >= {p.max_drawdown_pct}")
        return breaches

    def _common_state_checks(
        self, c: _Checks, *, mode: TradingMode, kill: bool, health: HealthView
    ) -> None:
        p = self.policy
        c.add(
            "trading_mode",
            mode in (TradingMode.PAPER, TradingMode.REPLAY, TradingMode.LIVE),
            str(mode),
        )
        c.add("kill_switch", not kill, "engaged" if kill else "")
        drift = health.clock_drift_ms
        c.add(
            "clock_drift",
            drift is not None and abs(drift) <= p.max_clock_drift_ms,
            "unknown" if drift is None else f"{drift}ms",
        )
        c.add("unknown_orders", health.unknown_orders == 0, f"{health.unknown_orders} unknown")
        c.add("watchdog", health.watchdog_ok)

    # ------------------------------------------------------------------ entries
    def evaluate_entry(
        self, candidate: TradeCandidate, snapshot: MarketSnapshot, ctx: EntryContext
    ) -> RiskDecision:
        p = self.policy
        now = self._clock.now_ms()
        c = _Checks()
        market = snapshot.market
        pf = ctx.portfolio

        # --- system state -------------------------------------------------------------
        self._common_state_checks(c, mode=ctx.mode, kill=ctx.kill_switch_engaged, health=ctx.health)
        expected_state = BotState.LIVE if ctx.mode is TradingMode.LIVE else BotState.PAPER
        c.add(
            "bot_state", ctx.bot_state is expected_state, f"{ctx.bot_state} (need {expected_state})"
        )
        breaches = self.loss_limit_breaches(pf)
        c.add("loss_limits", not breaches, "; ".join(breaches))
        c.add(
            "consecutive_losses",
            pf.consecutive_losses < p.max_consecutive_losses,
            f"{pf.consecutive_losses}",
        )
        c.add(
            "cooldown_after_loss",
            pf.last_loss_ms is None or now - pf.last_loss_ms >= p.cooldown_after_loss_s * 1000,
        )
        c.add("market_stream", ctx.health.market_stream_ok)
        c.add("reference_stream", ctx.health.reference_stream_ok)
        rec_ms = ctx.health.last_reconciliation_ms
        c.add(
            "reconciliation",
            ctx.health.last_reconciliation_ok
            and rec_ms is not None
            and now - rec_ms <= p.require_reconciled_within_s * 1000,
            "never"
            if rec_ms is None
            else f"age {now - rec_ms}ms ok={ctx.health.last_reconciliation_ok}",
        )

        # --- market / resolution ---------------------------------------------------------
        c.add("resolution_valid", ctx.resolution_valid, ctx.resolution_detail)
        c.add("rule_id_consistent", market.rule_id != "", market.rule_id)
        c.add("market_accepting_orders", market.accepting_orders)
        c.add("candidate_market", candidate.condition_id == market.condition_id)
        c.add("candidate_filters", candidate.passes_filters, "; ".join(candidate.rejections))
        c.add("entry_side_buy", candidate.side is Side.BUY)

        # --- data freshness --------------------------------------------------------------
        quote = snapshot.quote(candidate.outcome)
        c.add("snapshot_fresh", snapshot.is_fresh, "; ".join(snapshot.stale_reasons))
        c.add(
            "book_age",
            quote.book_valid
            and quote.book_age_ms is not None
            and quote.book_age_ms <= p.max_data_age_ms,
            f"{quote.book_age_ms}ms valid={quote.book_valid}",
        )
        ref = snapshot.reference
        ref_ages = [a for a in (ref.spot_age_ms, ref.twap_age_ms) if a is not None]
        c.add(
            "reference_age",
            len(ref_ages) == 2 and max(ref_ages) <= p.max_reference_age_ms,
            f"spot={ref.spot_age_ms} twap={ref.twap_age_ms}",
        )
        c.add("price_to_beat_verified", ref.price_to_beat_verified, str(ref.price_to_beat_source))
        c.add(
            "time_to_expiry",
            snapshot.time_to_expiry_ms >= p.min_time_to_expiry_s * 1000,
            f"{snapshot.time_to_expiry_ms}ms",
        )
        c.add(
            "time_since_start",
            snapshot.time_since_start_ms >= p.min_time_since_start_s * 1000,
            f"{snapshot.time_since_start_ms}ms",
        )

        # --- price / liquidity / edge ----------------------------------------------------
        spread = quote.spread
        c.add("spread", spread is not None and spread <= p.max_spread, f"{spread}")
        c.add(
            "slippage",
            candidate.estimated_slippage <= p.max_slippage,
            f"{candidate.estimated_slippage}",
        )
        c.add(
            "liquidity",
            candidate.liquidity_usd >= p.min_liquidity_usd,
            f"{candidate.liquidity_usd}",
        )
        limit = candidate.worst_price
        c.add(
            "entry_price_band",
            p.min_entry_price <= limit <= p.max_entry_price,
            f"{limit} not in [{p.min_entry_price}, {p.max_entry_price}]",
        )
        c.add("tick_size", _on_tick(limit, market.tick_size), f"{limit} tick {market.tick_size}")
        fee_rate = market.fee_schedule.fee_rate_at(limit)
        c.add(
            "fee_known", fee_rate >= 0 and market.fee_schedule.rate <= Decimal("0.2"), f"{fee_rate}"
        )
        min_edge = float(p.min_conservative_edge)
        c.add(
            "conservative_edge",
            candidate.conservative_edge >= min_edge,
            f"{candidate.conservative_edge:.4f} < {min_edge}"
            if candidate.conservative_edge < min_edge
            else "",
        )
        c.add(
            "worst_case_edge",
            candidate.worst_case_edge >= min_edge,
            f"{candidate.worst_case_edge:.4f}",
        )
        c.add(
            "uncertainty",
            candidate.uncertainty <= float(p.max_uncertainty),
            f"{candidate.uncertainty:.4f}",
        )
        c.add(
            "probability_bounds_sane",
            0.0
            <= candidate.probability_lower
            <= candidate.fair_probability
            <= candidate.probability_upper
            <= 1.0,
        )

        # --- sizing ----------------------------------------------------------------------
        mult = ctx.llm_size_multiplier
        c.add("llm_multiplier_range", ZERO <= mult <= ONE, str(mult))
        existing = pf.positions.get(candidate.token_id)
        existing_cost = existing.cost_basis_usd if existing else ZERO
        tokens_in_market = {
            pos.token_id
            for pos in pf.positions.values()
            if pos.condition_id == candidate.condition_id and pos.shares > 0
        } | {candidate.token_id}
        c.add(
            "positions_per_market",
            len(tokens_in_market) <= p.max_positions_per_market,
            f"{len(tokens_in_market)} tokens in market",
        )
        open_count = sum(1 for pos in pf.positions.values() if pos.shares > 0)
        c.add(
            "max_open_positions",
            existing is not None or open_count < p.max_open_positions,
            f"{open_count}",
        )
        available_cash = pf.cash_usd - pf.pending_buy_usd - p.min_balance_reserve_usd
        # Reserve the worst-case fee on top of the notional.
        fee_factor = ONE + fee_rate / limit if limit > 0 else ONE
        caps = {
            "candidate": candidate.notional_usd,
            "order": p.max_order_size_usd,
            "position": p.max_position_usd - existing_cost,
            "exposure": p.max_total_exposure_usd - pf.exposure_usd,
            "cash": (available_cash / fee_factor) if available_cash > 0 else ZERO,
        }
        size = min(caps.values()) * (mult if ZERO <= mult <= ONE else ZERO)
        size = size.quantize(Decimal("0.01"), rounding=ROUND_FLOOR) if size > 0 else ZERO
        binding = min(caps, key=lambda k: caps[k])
        c.add("position_limit", caps["position"] > 0, f"remaining {caps['position']}")
        c.add("exposure_limit", caps["exposure"] > 0, f"remaining {caps['exposure']}")
        c.add("balance", caps["cash"] > 0, f"available {available_cash}")
        c.add("min_notional", size >= MIN_ORDER_NOTIONAL_USD, f"size {size} (bound by {binding})")
        shares_at_limit = (size / limit) if limit > 0 else ZERO
        c.add(
            "min_order_size",
            shares_at_limit >= market.min_order_size,
            f"{shares_at_limit:.2f} shares < {market.min_order_size}"
            if shares_at_limit < market.min_order_size
            else "",
        )

        # --- duplicates / rate limits -----------------------------------------------------
        c.add(
            "in_flight",
            ctx.health.in_flight_orders < ctx.max_in_flight_orders
            and candidate.token_id not in ctx.health.in_flight_tokens,
            f"{ctx.health.in_flight_orders} in flight",
        )
        last_submit = ctx.rates.last_submit_ms_by_market.get(candidate.condition_id)
        c.add(
            "cooldown_same_market",
            last_submit is None or now - last_submit >= p.cooldown_same_market_s * 1000,
            "" if last_submit is None else f"{now - last_submit}ms since last order",
        )
        c.add("rate_minute", ctx.rates.submissions_last_minute < p.max_trades_per_minute)
        c.add("rate_hour", ctx.rates.submissions_last_hour < p.max_trades_per_hour)
        c.add("rate_day", ctx.rates.submissions_last_day < p.max_trades_per_day)

        allowed = not c.failed
        return RiskDecision(
            decision_id=f"rd-{uuid.uuid4().hex}",
            allowed=allowed,
            reasons=c.failed if not allowed else (f"size bound by {binding}",),
            checks=tuple(c.items),
            purpose=OrderPurpose.ENTRY,
            side=Side.BUY,
            token_id=candidate.token_id,
            condition_id=candidate.condition_id,
            max_size_usd=size if allowed else ZERO,
            max_shares=shares_at_limit.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
            if allowed
            else ZERO,
            limit_price=limit,
            risk_version=RISK_ENGINE_VERSION,
            policy_hash=self.policy_hash,
            timestamp_ms=now,
            candidate_id=candidate.candidate_id,
        )

    # ------------------------------------------------------------------ exits
    def evaluate_exit(
        self, signal: ExitSignal, snapshot: MarketSnapshot | None, ctx: ExitContext
    ) -> RiskDecision:
        """Reduce-only SELL. Allowed in HALTED (policy) and KILL_SWITCH (exit policy)."""
        p = self.policy
        now = self._clock.now_ms()
        c = _Checks()
        c.add("trading_mode", ctx.mode in (TradingMode.PAPER, TradingMode.REPLAY, TradingMode.LIVE))
        state_ok = (
            ctx.bot_state in (BotState.PAPER, BotState.LIVE)
            or (ctx.bot_state is BotState.HALTED and p.allow_exits_when_halted)
            or (ctx.bot_state is BotState.KILL_SWITCH and ctx.exit_allowed_in_kill_switch)
        )
        c.add("bot_state", state_ok, str(ctx.bot_state))
        c.add(
            "kill_switch",
            not ctx.kill_switch_engaged or ctx.exit_allowed_in_kill_switch,
            "engaged; exits disabled by policy" if ctx.kill_switch_engaged else "",
        )
        c.add("unknown_orders", ctx.health.unknown_orders == 0, f"{ctx.health.unknown_orders}")
        held = ctx.portfolio.positions.get(signal.token_id)
        held_shares = held.shares if held else ZERO
        pending = ctx.portfolio.pending_sell_shares.get(signal.token_id, ZERO)
        sellable = held_shares - pending
        c.add(
            "reduce_only", ZERO < signal.shares <= sellable, f"sell {signal.shares} of {sellable}"
        )
        c.add("min_price_set", signal.min_price is not None, "cannot price safely")
        c.add("in_flight", signal.token_id not in ctx.health.in_flight_tokens)
        c.add("rate_minute_exits", ctx.rates.exits_last_minute < p.max_trades_per_minute * 2)
        if snapshot is not None:
            market = snapshot.market
            c.add("min_order_size", signal.shares >= market.min_order_size, f"{signal.shares}")
            if signal.min_price is not None:
                c.add(
                    "tick_size", _on_tick(signal.min_price, market.tick_size), f"{signal.min_price}"
                )
            if p.exits_require_fresh_book:
                quote = snapshot.quote(market.outcome_of(signal.token_id))
                c.add(
                    "book_age",
                    quote.book_valid
                    and quote.book_age_ms is not None
                    and quote.book_age_ms <= p.max_data_age_ms,
                    f"{quote.book_age_ms}ms",
                )
        else:
            c.add("snapshot_available", False, "no market snapshot")
        allowed = not c.failed
        min_price = signal.min_price if signal.min_price is not None else ONE - Decimal("0.0001")
        return RiskDecision(
            decision_id=f"rd-{uuid.uuid4().hex}",
            allowed=allowed,
            reasons=c.failed if not allowed else signal.reasons,
            checks=tuple(c.items),
            purpose=OrderPurpose.EXIT,
            side=Side.SELL,
            token_id=signal.token_id,
            condition_id=signal.condition_id,
            max_size_usd=ZERO,
            max_shares=signal.shares if allowed else ZERO,
            limit_price=min_price,
            risk_version=RISK_ENGINE_VERSION,
            policy_hash=self.policy_hash,
            timestamp_ms=now,
        )


def _on_tick(price: Decimal, tick: Decimal) -> bool:
    return tick > 0 and (price / tick) == (price / tick).to_integral_value()
