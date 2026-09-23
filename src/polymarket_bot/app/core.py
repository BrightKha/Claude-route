"""Trading core: one deterministic decision step, identical in paper, replay and live.

The core is driven from outside: :meth:`TradingCore.on_message` for every raw
market-data message and :meth:`TradingCore.step` on every decision tick. It owns
no sockets and no timers, so the replay engine can drive it with a simulated
clock and the live runner with the system clock (same code path, no lookahead).

Order of one step (docs/architecture.md "Decision step"):

1. poll execution events (fills are applied to the portfolio idempotently);
2. settle resolved markets, mark positions at best bid, check loss limits;
3. resolve UNKNOWN orders, reconcile with the venue when due;
4. advance the lifecycle (SYNCING -> PAPER/LIVE, HALTED -> SYNCING when safe);
5. exits (never blocked by Claude), then entries, then MCP proposals;
6. publish status for the CLI / MCP server.

Any invariant violation engages the kill switch. Any other unexpected exception
is counted; the watchdog then halts for the operator (fail closed).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from polymarket_bot.audit.audit_log import AuditLog
from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.data.recorder import SessionRecorder
from polymarket_bot.domain.clock import Clock
from polymarket_bot.domain.decisions import RiskDecision, TradeCandidate
from polymarket_bot.domain.market import MarketDefinition
from polymarket_bot.domain.orders import Fill, OrderRecord
from polymarket_bot.domain.snapshot import MarketSnapshot
from polymarket_bot.domain.types import BotState, OrderPurpose, TradingMode
from polymarket_bot.execution.engine import (
    ExecutionEngine,
    ExecutionError,
    InvariantViolationError,
)
from polymarket_bot.exits.engine import ExitEngine, ExitEvaluation, HeldPosition
from polymarket_bot.features.btc5m import FeatureVector, compute_features
from polymarket_bot.lifecycle.kill_switch import KillSwitch
from polymarket_bot.lifecycle.state_machine import BotStateMachine, LiveAuthorization
from polymarket_bot.llm.reviewer import CandidateReviewer, ReviewVerdict
from polymarket_bot.llm.schemas import build_review_context
from polymarket_bot.market.hub import MarketDataHub
from polymarket_bot.portfolio.portfolio import ClosedTrade, InvariantError, Portfolio
from polymarket_bot.ports import AccountProvider, RawMessage, TradingProvider
from polymarket_bot.reconciliation.reconciler import LocalState, Reconciler
from polymarket_bot.risk.engine import EntryContext, ExitContext, HealthView, RiskEngine
from polymarket_bot.risk.rates import RateTracker
from polymarket_bot.signals.edge import EdgeEngine
from polymarket_bot.storage.sqlite_store import ProposalInbox, StateStore
from polymarket_bot.strategies.btc_5m.fair_value import FairValueEngine, FairValueEstimate
from polymarket_bot.watchdog.health import HealthRegistry
from polymarket_bot.watchdog.watchdog import Watchdog

log = logging.getLogger(__name__)

ZERO = Decimal(0)
HOUR_MS = 3_600_000
UNRESOLVED_ALERT_MS = 30 * 60_000
RECONCILE_FAILURES_BEFORE_HALT = 2


@dataclass
class CoreDeps:
    config: AppConfig
    mode: TradingMode
    clock: Clock
    store: StateStore
    audit: AuditLog
    health: HealthRegistry
    state: BotStateMachine
    kill_switch: KillSwitch
    hub: MarketDataHub
    trading: TradingProvider
    account: AccountProvider
    risk: RiskEngine
    fair_value: FairValueEngine
    watchdog: Watchdog
    initial_cash_usd: Decimal
    reviewer: CandidateReviewer | None = None
    inbox: ProposalInbox | None = None
    recorder: SessionRecorder | None = None
    settle_venue: Any = None  # PaperExchange in paper/replay (simulated redemption)
    live_authorization: LiveAuthorization | None = None
    publish_interval_ms: int = 2_000


@dataclass
class EntryMeta:
    """Decision-time facts about an entry, kept for exits and attribution."""

    fair_probability: float
    probability_lower: float
    conservative_edge: float
    market_price: float  # executable price at decision = market-implied probability
    sigma_bps: float | None  # realised vol at decision (bps per sqrt second)
    time_to_expiry_ms: int
    decided_ms: int
    max_holding_s: int | None
    source: str


@dataclass(frozen=True)
class TradeLog:
    trade: ClosedTrade
    meta: EntryMeta | None


@dataclass
class CoreStats:
    steps: int = 0
    candidates_passing: int = 0
    entries_submitted: int = 0
    exits_submitted: int = 0
    risk_rejections: int = 0
    llm_blocked: int = 0
    settlements: int = 0
    # (model probability, market-implied probability, won) per filled entry
    predictions: list[tuple[float, float, int]] = field(default_factory=list)
    reject_reasons: dict[str, int] = field(default_factory=dict)


class TradingCore:
    def __init__(self, deps: CoreDeps) -> None:
        self.d = deps
        cfg = deps.config
        self.portfolio = Portfolio.with_cash(deps.initial_cash_usd, deps.clock.now_ms())
        self.execution = ExecutionEngine(
            deps.trading,
            deps.store,
            deps.audit,
            deps.clock,
            cfg.execution,
            on_fill=self._on_fill,
        )
        self.edge = EdgeEngine(cfg.edge, deps.risk.policy)
        self.exits = ExitEngine(cfg.exits)
        self.reconciler = Reconciler(cfg.reconciliation)
        self.rates = RateTracker()
        self.stats = CoreStats()
        self.account_anomaly = False
        self._recon_failures = 0
        self._last_recon_ms: int | None = None
        self._last_publish_ms: int | None = None
        self._last_equity_mark_ms: int | None = None
        self._entry_meta: dict[str, EntryMeta] = {}  # token -> meta of the open position
        self._intent_meta: dict[str, EntryMeta] = {}  # entry intent -> meta until it fills
        # cid -> [(outcome, model p, market p)]
        self._open_predictions: dict[str, list[tuple[str, float, float]]] = {}
        self.trade_log: list[TradeLog] = []
        self._pending_redemption: dict[str, Decimal] = {}
        self._alerted_unresolved: set[str] = set()
        self._violations_seen = 0
        self._recoveries: deque[int] = deque()
        self._review_tasks: set[asyncio.Task[None]] = set()
        self.last_candidates: dict[str, list[TradeCandidate]] = {}
        self._outcome_by_token: dict[str, str] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        d = self.d
        d.kill_switch.sync_from_persistence()
        unknown = self.execution.load_open_orders()
        d.health.unknown_orders(self.execution.unknown_count())
        d.store.set_meta("risk_policy_hash", d.risk.policy_hash)
        d.audit.append(
            "core_start",
            {
                "mode": d.mode.value,
                "policy_hash": d.risk.policy_hash,
                "strategy": d.config.strategy.version,
                "unknown_orders_at_start": unknown,
            },
        )
        if d.state.state is BotState.KILL_SWITCH:
            log.critical("kill switch engaged at startup: %s", d.kill_switch.reason())
            return
        if d.mode is TradingMode.LIVE and d.live_authorization is None:
            raise RuntimeError("live mode requires a LiveAuthorization from the live lock")
        d.state.transition(BotState.INITIALIZING, "startup")
        d.state.transition(BotState.SYNCING, "waiting for market data and reconciliation")

    def on_message(self, msg: RawMessage) -> list[str]:
        """Apply one market-data message; returns token ids to subscribe."""
        return self.d.hub.on_raw(msg)

    async def step(self) -> None:
        d = self.d
        now = d.clock.now_ms()
        d.health.beat_loop()
        self.stats.steps += 1
        try:
            await self._poll_execution()
            self._settle(now)
            self._mark(now)
            self._check_loss_limits()
            await self._resolve_unknown()
            await self._maybe_reconcile(now)
            self._advance_lifecycle(now)
            await self._exits(now)
            await self._entries(now)
            await self._proposals(now)
            self._publish(now)
            d.health.decision(now)
        except (InvariantError, InvariantViolationError) as exc:
            self._engage_kill(f"invariant violation: {exc}")
        except Exception as exc:
            log.exception("decision step failed")
            d.health.exception()
            d.store.insert_incident(
                now, "critical", "step_exception", {"error": type(exc).__name__}
            )

    async def shutdown(self) -> None:
        for task in list(self._review_tasks):
            task.cancel()
        if self.execution.open_orders():
            try:
                await self.d.trading.cancel_all()
            except Exception:
                log.exception("cancel_all at shutdown failed")
        self._publish(self.d.clock.now_ms(), force=True)
        self.d.audit.append("core_stop", {"state": self.d.state.state.value})

    # ------------------------------------------------------------------ execution
    def _on_fill(self, fill: Fill, record: OrderRecord) -> None:
        market = self.d.hub.markets.get(fill.condition_id)
        end_ms = market.definition.window_end_ms if market else 0
        closed = self.portfolio.apply_fill(
            fill, market_slug=record.intent.market_slug, window_end_ms=end_ms
        )
        self._outcome_by_token[fill.token_id] = fill.outcome
        meta = self._intent_meta.pop(record.intent.intent_id, None)
        if meta is not None:  # first fill of an entry order
            self._entry_meta.setdefault(fill.token_id, meta)
            self._open_predictions.setdefault(fill.condition_id, []).append(
                (fill.outcome, meta.fair_probability, meta.market_price)
            )
        if closed is not None:
            self.trade_log.append(TradeLog(closed, self._entry_meta.pop(fill.token_id, None)))
        self.d.audit.append("portfolio_fill", {"fill": fill, "closed": closed})
        if self.d.recorder is not None:
            self.d.recorder.record_bot("fill", {"fill": fill, "closed": closed})

    async def _poll_execution(self) -> None:
        events = await self.d.trading.poll_events()
        if events:
            self.execution.apply_events(events)
        for intent_id in list(self._intent_meta):  # entries that ended without any fill
            rec = self.execution.orders.get(intent_id)
            if rec is not None and rec.status.is_terminal:
                del self._intent_meta[intent_id]
        self._check_violations()
        self.d.health.unknown_orders(self.execution.unknown_count())

    def _check_violations(self) -> None:
        violations = self.execution.violations
        if len(violations) > self._violations_seen:
            new = violations[self._violations_seen :]
            self._violations_seen = len(violations)
            self._engage_kill("execution invariant: " + "; ".join(new))

    async def _resolve_unknown(self) -> None:
        if self.execution.unknown_count() == 0:
            return
        stuck = await self.execution.resolve_unknown()
        self._check_violations()
        if stuck:
            self.d.store.insert_incident(
                self.d.clock.now_ms(), "critical", "unknown_order_timeout", {"intents": stuck}
            )
            self.d.state.halt("order state unknown past timeout", manual_only=True)

    def _engage_kill(self, reason: str) -> None:
        log.critical("engaging kill switch: %s", reason)
        self.d.kill_switch.engage(reason, source="core")

    # ------------------------------------------------------------------ portfolio
    def _settle(self, now: int) -> None:
        d = self.d
        held = {p.condition_id for p in self.portfolio.positions.values()}
        for cid in held | set(self._open_predictions):
            tracked = d.hub.markets.get(cid)
            if tracked is None:
                continue
            if tracked.winner is not None:
                for outcome, p, market_p in self._open_predictions.pop(cid, []):
                    self.stats.predictions.append((p, market_p, int(outcome == tracked.winner)))
            if cid not in held:
                continue
            if tracked.winner is None:
                end = tracked.definition.window_end_ms
                if now - end > UNRESOLVED_ALERT_MS and cid not in self._alerted_unresolved:
                    self._alerted_unresolved.add(cid)
                    d.store.insert_incident(
                        now, "warning", "unresolved_market", {"slug": tracked.definition.slug}
                    )
                continue
            if not tracked.resolution_consistent:
                d.store.insert_incident(
                    now, "critical", "resolution_anomaly", {"slug": tracked.definition.slug}
                )
                d.state.halt("official outcome inconsistent with the rule", manual_only=True)
            market = tracked.definition
            winner_token = next(t.token_id for t in market.tokens if t.outcome == tracked.winner)
            loser_token = market.other(winner_token)
            held_winner = self.portfolio.positions.get(winner_token)
            payout = held_winner.shares if held_winner else ZERO
            trades = self.portfolio.settle(cid, tracked.winner, now)
            if d.settle_venue is not None:
                d.settle_venue.settle(winner_token, loser_token)
            elif payout > 0:
                self._pending_redemption[winner_token] = payout  # live: redeem on-chain
            d.store.record_settlement(cid, tracked.winner, now, "gamma")
            for trade in trades:
                self.trade_log.append(TradeLog(trade, self._entry_meta.pop(trade.token_id, None)))
            self.stats.settlements += len(trades)
            d.audit.append(
                "settlement", {"slug": market.slug, "winner": tracked.winner, "trades": trades}
            )
            if d.recorder is not None:
                d.recorder.record_bot("settlement", {"slug": market.slug, "trades": trades})

    def _mark(self, now: int) -> None:
        bids: dict[str, Decimal | None] = {}
        for token in self.portfolio.positions:
            book = self.d.hub.books.get(token)
            snap = book.snapshot() if book else None
            bids[token] = snap.best_bid if snap else None
        self.portfolio.mark(bids, now)
        self.d.health.pnl_update(now)
        if self._last_equity_mark_ms is None or now - self._last_equity_mark_ms >= 10_000:
            self._last_equity_mark_ms = now
            self.d.store.insert_equity_mark(
                ts_ms=now,
                equity=self.portfolio.equity_usd,
                cash=self.portfolio.cash_usd,
                exposure=self.portfolio.exposure_usd,
                daily_pnl=self.portfolio.equity_usd - self.portfolio.start_of_day_equity_usd,
            )

    def _check_loss_limits(self) -> None:
        if self.d.kill_switch.is_engaged():
            return
        breaches = self.d.risk.loss_limit_breaches(self._portfolio_view())
        if breaches:
            self._engage_kill("loss limit: " + "; ".join(breaches))

    def _portfolio_view(self) -> Any:
        return self.portfolio.view(
            pending_buy_usd=self.execution.pending_buy_usd(),
            pending_sell_shares=self.execution.pending_sell_shares(),
        )

    # ------------------------------------------------------------------ reconciliation
    async def _maybe_reconcile(self, now: int) -> None:
        interval = self.d.config.reconciliation.interval_s * 1000
        due = self._last_recon_ms is None or now - self._last_recon_ms >= interval
        if not due or self.execution.open_orders():
            return  # never compare while an order may still change either ledger
        self._last_recon_ms = now
        try:
            remote = await self.d.account.account_snapshot()
        except Exception as exc:
            log.warning("account snapshot failed: %s", type(exc).__name__)
            self.d.health.reconciliation(ts_ms=now, ok=False)
            return
        for token in [t for t in self._pending_redemption if remote.positions.get(t, ZERO) <= 0]:
            del self._pending_redemption[token]  # redeemed on-chain
        pending_cash = sum(self._pending_redemption.values(), ZERO)
        local = LocalState(
            positions={t: p.shares for t, p in self.portfolio.positions.items()},
            cash_usd=self.portfolio.cash_usd - pending_cash,
            open_order_ids=frozenset(
                r.exchange_order_id for r in self.execution.open_orders() if r.exchange_order_id
            ),
            known_order_ids=self.execution.known_exchange_ids(),
            ignore_tokens=frozenset(self._pending_redemption),
        )
        report = self.reconciler.compare(local, remote, now)
        self.d.store.insert_reconciliation(now, report.ok, report)
        self.d.health.reconciliation(ts_ms=now, ok=report.ok)
        if report.ok:
            self._recon_failures = 0
            return
        self._recon_failures += 1
        self.d.store.insert_incident(now, "critical", "reconciliation_mismatch", report)
        if self._recon_failures >= RECONCILE_FAILURES_BEFORE_HALT:
            self.account_anomaly = True
            self.d.state.halt("reconciliation mismatch (confirmed)", manual_only=True)
        else:
            self._last_recon_ms = None  # re-check on the next step before escalating

    # ------------------------------------------------------------------ lifecycle
    def _advance_lifecycle(self, now: int) -> None:
        d = self.d
        state = d.state.state
        if state is BotState.SYNCING:
            if self._last_recon_ms is None or self._recon_failures:
                return
            if d.watchdog.recovery_blockers():
                return
            if d.mode is TradingMode.LIVE:
                d.state.transition(BotState.LIVE, "synced", live_authorization=d.live_authorization)
            else:
                d.state.transition(BotState.PAPER, "synced")
        elif state is BotState.HALTED and not d.state.manual_only:
            while self._recoveries and now - self._recoveries[0] > HOUR_MS:
                self._recoveries.popleft()
            if len(self._recoveries) >= d.config.watchdog.max_auto_recoveries_per_hour:
                d.state.halt("too many automatic recoveries", manual_only=True)
                return
            if not d.watchdog.recovery_blockers():
                self._recoveries.append(now)
                d.state.transition(BotState.SYNCING, "auto recovery: blockers cleared")

    # ------------------------------------------------------------------ strategy helpers
    def _evaluate(self, snap: MarketSnapshot) -> tuple[FeatureVector, FairValueEstimate]:
        fv = compute_features(
            snap, self.d.hub.reference, twap_lookback_s=self.d.config.fair_value.twap_lookback_s
        )
        return fv, self.d.fair_value.estimate(fv)

    def _health_view(self) -> HealthView:
        h = self.d.health.snapshot(self.d.clock.now_ms())
        open_orders = self.execution.open_orders()
        return HealthView(
            market_stream_ok=h.market_stream_connected,
            reference_stream_ok=h.reference_stream_connected,
            clock_drift_ms=h.clock_drift_ms,
            last_reconciliation_ms=h.last_reconciliation_ms,
            last_reconciliation_ok=h.last_reconciliation_ok,
            watchdog_ok=self.d.watchdog.healthy,
            unknown_orders=self.execution.unknown_count(),
            in_flight_orders=len(open_orders),
            in_flight_tokens=self.execution.in_flight_tokens(),
        )

    def _count_reject(self, reason: str) -> None:
        key = reason.split(":", maxsplit=1)[0][:48]
        self.stats.reject_reasons[key] = self.stats.reject_reasons.get(key, 0) + 1

    # ------------------------------------------------------------------ exits
    async def _exits(self, now: int) -> None:
        d = self.d
        in_flight = self.execution.in_flight_tokens()
        for token, pos in list(self.portfolio.positions.items()):
            if token in in_flight or token in self._pending_redemption:
                continue
            tracked = d.hub.markets.get(pos.condition_id)
            snap = d.hub.snapshot(pos.condition_id) if tracked else None
            estimate = self._evaluate(snap)[1] if snap else None
            held = self._held(token)
            evaluation = self.exits.evaluate(
                held,
                snap,
                estimate,
                bot_state=d.state.state,
                kill_switch=d.kill_switch.is_engaged(),
                account_anomaly=self.account_anomaly,
                now_ms=now,
            )
            await self._submit_exit(evaluation, snap, pos.outcome)

    def _held(self, token: str) -> HeldPosition:
        pos = self.portfolio.positions[token]
        meta = self._entry_meta.get(token)
        return HeldPosition(
            token_id=token,
            condition_id=pos.condition_id,
            outcome=pos.outcome,
            shares=pos.shares,
            avg_entry_price=pos.avg_cost_per_share,
            opened_ms=pos.opened_ms,
            max_holding_s=meta.max_holding_s if meta else None,
        )

    async def _submit_exit(
        self, evaluation: ExitEvaluation, snap: MarketSnapshot | None, outcome: str
    ) -> RiskDecision | None:
        d = self.d
        if evaluation.action != "EXIT" or evaluation.signal is None or snap is None:
            return None
        ctx = ExitContext(
            mode=d.mode,
            bot_state=d.state.state,
            kill_switch_engaged=d.kill_switch.is_engaged(),
            portfolio=self._portfolio_view(),
            health=self._health_view(),
            rates=self.rates.view(d.clock.now_ms()),
            exit_allowed_in_kill_switch=d.config.exits.on_kill_switch == "exit_if_priced",
        )
        decision = d.risk.evaluate_exit(evaluation.signal, snap, ctx)
        d.store.insert_risk_decision(decision)
        if not decision.allowed:
            self._count_reject(decision.reasons[0] if decision.reasons else "exit rejected")
            return decision
        await self._execute(decision, snap.market, outcome, OrderPurpose.EXIT)
        return decision

    # ------------------------------------------------------------------ entries
    async def _entries(self, now: int) -> None:
        d = self.d
        can_enter = d.state.can_open_positions() and not d.kill_switch.is_engaged()
        for tracked in d.hub.active_markets(now):
            cid = tracked.definition.condition_id
            snap = d.hub.snapshot(cid)
            if snap is None:
                continue
            fv, estimate = self._evaluate(snap)
            candidates = self.edge.candidates(snap, fv, estimate, resolution_valid=True)
            self.last_candidates[cid] = candidates
            if not can_enter:
                continue
            for cand in candidates:
                if cand.passes_filters:
                    self.stats.candidates_passing += 1
                    await self._try_entry(cand, snap, fv, source="strategy")

    def _verdict(self, cand: TradeCandidate, now: int) -> ReviewVerdict:
        if self.d.reviewer is None:
            return ReviewVerdict("llm_off", True, cand, ("no reviewer in this mode",))
        return self.d.reviewer.verdict_for(cand, now)

    def _schedule_review(
        self, cand: TradeCandidate, snap: MarketSnapshot, fv: FeatureVector
    ) -> None:
        reviewer = self.d.reviewer
        if reviewer is None:
            return
        sigma = fv.sigma_per_sqrt_s
        context = build_review_context(
            cand, snap, volatility_bps_per_sqrt_s=None if sigma is None else sigma * 1e4
        )
        task = asyncio.create_task(reviewer.run_review(cand, context))
        self._review_tasks.add(task)
        task.add_done_callback(self._review_tasks.discard)

    async def _try_entry(
        self, cand: TradeCandidate, snap: MarketSnapshot, fv: FeatureVector, *, source: str
    ) -> str:
        d = self.d
        now = d.clock.now_ms()
        verdict = self._verdict(cand, now)
        if verdict.status == "needs_call":
            self._schedule_review(cand, snap, fv)
            return "llm review requested"
        if verdict.status == "pending":
            return "llm review pending"
        if not verdict.allowed:
            self.stats.llm_blocked += 1
            return verdict.reasons[0] if verdict.reasons else "blocked by llm policy"
        ctx = EntryContext(
            mode=d.mode,
            bot_state=d.state.state,
            kill_switch_engaged=d.kill_switch.is_engaged(),
            resolution_valid=True,
            resolution_detail=snap.market.rule_id,
            portfolio=self._portfolio_view(),
            health=self._health_view(),
            rates=self.rates.view(now),
            max_in_flight_orders=d.config.execution.max_in_flight_orders,
        )
        decision = d.risk.evaluate_entry(verdict.candidate, snap, ctx)
        d.store.insert_risk_decision(decision)
        if not decision.allowed:
            self.stats.risk_rejections += 1
            self._count_reject(decision.reasons[0] if decision.reasons else "rejected")
            return "risk: " + "; ".join(decision.reasons[:3])
        d.store.insert_candidate(
            candidate_id=cand.candidate_id,
            ts_ms=now,
            condition_id=cand.condition_id,
            outcome=cand.outcome,
            passes=True,
            body={"candidate": verdict.candidate, "verdict": verdict.status, "source": source},
        )
        record = await self._execute(decision, snap.market, cand.outcome, OrderPurpose.ENTRY)
        if record is None:
            return "execution refused"
        tightened = verdict.candidate
        self._intent_meta[record.intent.intent_id] = EntryMeta(
            fair_probability=tightened.fair_probability,
            probability_lower=tightened.probability_lower,
            conservative_edge=tightened.conservative_edge,
            market_price=float(tightened.executable_price),
            sigma_bps=None if fv.sigma_per_sqrt_s is None else fv.sigma_per_sqrt_s * 1e4,
            time_to_expiry_ms=snap.time_to_expiry_ms,
            decided_ms=now,
            max_holding_s=verdict.max_holding_s,
            source=source,
        )
        return f"submitted {record.intent.intent_id}"

    async def _execute(
        self,
        decision: RiskDecision,
        market: MarketDefinition,
        outcome: str,
        purpose: OrderPurpose,
    ) -> OrderRecord | None:
        d = self.d
        try:
            record = await self.execution.execute(decision, market, outcome)
        except ExecutionError as exc:
            log.warning("execution refused: %s", exc)
            self._count_reject(f"execution: {exc}")
            return None
        self.rates.record(d.clock.now_ms(), decision.condition_id, purpose)
        if purpose is OrderPurpose.ENTRY:
            self.stats.entries_submitted += 1
        else:
            self.stats.exits_submitted += 1
        if d.recorder is not None:
            d.recorder.record_bot("order", {"decision": decision, "record": record})
        return record

    # ------------------------------------------------------------------ MCP proposals
    async def _proposals(self, now: int) -> None:
        inbox = self.d.inbox
        if inbox is None:
            return
        ttl_ms = self.d.config.mcp.proposal_ttl_s * 1000
        for proposal in inbox.pending():
            pid = str(proposal["proposal_id"])
            if now - int(proposal["created_ms"]) > ttl_ms:
                inbox.mark(pid, "EXPIRED", "not processed within ttl", now)
                continue
            payload = proposal["payload"]
            try:
                status, reason = await self._handle_proposal(str(proposal["kind"]), payload)
            except (KeyError, TypeError, ValueError) as exc:
                status, reason = "REJECTED", f"malformed proposal: {type(exc).__name__}"
            inbox.mark(pid, status, reason, now)
            self.d.audit.append(
                "mcp_proposal", {"proposal_id": pid, "status": status, "reason": reason}
            )

    def _market_by_slug(self, slug: str) -> MarketDefinition | None:
        for tracked in self.d.hub.markets.values():
            if tracked.definition.slug == slug:
                return tracked.definition
        return None

    async def _handle_proposal(self, kind: str, payload: dict[str, Any]) -> tuple[str, str]:
        market = self._market_by_slug(str(payload["market_slug"]))
        if market is None:
            return "REJECTED", "unknown or unvalidated market"
        outcome = str(payload["outcome"])
        token = next((t.token_id for t in market.tokens if t.outcome == outcome), None)
        if token is None:
            return "REJECTED", "unknown outcome"
        snap = self.d.hub.snapshot(market.condition_id)
        if snap is None:
            return "REJECTED", "no market snapshot"
        fv, estimate = self._evaluate(snap)
        if kind == "close":
            if token not in self.portfolio.positions:
                return "REJECTED", "no position to close"
            evaluation = self.exits.forced_exit(
                self._held(token), snap, estimate, reason="mcp close proposal", now_ms=snap.utc_ms
            )
            if evaluation.action != "EXIT":
                return "REJECTED", "; ".join(evaluation.reasons)
            decision = await self._submit_exit(evaluation, snap, outcome)
            if decision is None or not decision.allowed:
                return "REJECTED", "risk engine refused the exit"
            return "ACCEPTED", "exit submitted"
        if kind != "trade":
            return "REJECTED", f"unknown proposal kind {kind}"
        if not self.d.state.can_open_positions():
            return "REJECTED", f"bot state {self.d.state.state.value}"
        candidates = self.edge.candidates(snap, fv, estimate, resolution_valid=True)
        cand = next((c for c in candidates if c.outcome == outcome), None)
        if cand is None or not cand.passes_filters:
            why = "; ".join(cand.rejections) if cand else "no candidate"
            return "REJECTED", f"no deterministic edge: {why}"
        cap = Decimal(str(payload["max_notional_usd"]))
        sized = replace(
            cand,
            notional_usd=min(cand.notional_usd, cap),
            max_allowed_size_usd=min(cand.max_allowed_size_usd, cap),
        )
        result = await self._try_entry(sized, snap, fv, source="mcp")
        status = "ACCEPTED" if result.startswith("submitted") else "REJECTED"
        return status, result

    # ------------------------------------------------------------------ status
    def _publish(self, now: int, *, force: bool = False) -> None:
        d = self.d
        if (
            not force
            and self._last_publish_ms is not None
            and now - self._last_publish_ms < d.publish_interval_ms
        ):
            return
        self._last_publish_ms = now
        store = d.store
        pf = self.portfolio
        store.publish_status(
            "runtime",
            ts_ms=now,
            body={
                "mode": d.mode.value,
                "state": d.state.state.value,
                "state_reason": d.state.reason,
                "steps": self.stats.steps,
            },
        )
        store.publish_status(
            "portfolio",
            ts_ms=now,
            body={
                "cash_usd": pf.cash_usd,
                "equity_usd": pf.equity_usd,
                "exposure_usd": pf.exposure_usd,
                "positions": [
                    {
                        "market_slug": p.market_slug,
                        "outcome": p.outcome,
                        "shares": p.shares,
                        "cost_basis_usd": p.cost_basis_usd,
                        "mark": pf.marks.get(t),
                    }
                    for t, p in pf.positions.items()
                ],
                "pending_redemption_usd": sum(self._pending_redemption.values(), ZERO),
            },
        )
        store.publish_status(
            "pnl",
            ts_ms=now,
            body={
                "realized_pnl_usd": pf.realized_pnl_usd,
                "fees_paid_usd": pf.fees_paid_usd,
                "daily_pnl_usd": pf.equity_usd - pf.start_of_day_equity_usd,
                "closed_trades": len(pf.closed_trades),
                "consecutive_losses": pf.consecutive_losses,
            },
        )
        store.publish_status("candidates", ts_ms=now, body=self._candidate_status())
        store.publish_status("market", ts_ms=now, body=self._market_status(now))
        health = d.health.snapshot(now)
        store.publish_status(
            "health",
            ts_ms=now,
            body={
                "health": health,
                "watchdog_anomalies": [a.code for a in d.watchdog.last_anomalies],
                "account_anomaly": self.account_anomaly,
            },
        )
        store.publish_status(
            "strategy",
            ts_ms=now,
            body={
                "name": d.config.strategy.name,
                "version": d.config.strategy.version,
                "enabled": d.config.strategy.enabled,
                "rule_ids": d.config.strategy.enabled_rule_ids,
                "model_version": d.fair_value.version,
                "llm_mode": d.config.llm.mode if d.reviewer is not None else "off",
            },
        )
        store.publish_status("model_metrics", ts_ms=now, body=self.model_metrics())

    def _candidate_status(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for cands in self.last_candidates.values():
            out.extend(
                {
                    "market_slug": c.market_slug,
                    "outcome": c.outcome,
                    "fair_probability": round(c.fair_probability, 4),
                    "band": [round(c.probability_lower, 4), round(c.probability_upper, 4)],
                    "executable_price": c.executable_price,
                    "conservative_edge": round(c.conservative_edge, 4),
                    "passes": c.passes_filters,
                    "rejections": list(c.rejections[:3]),
                }
                for c in cands
            )
        return out[-20:]

    def _market_status(self, now: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tracked in self.d.hub.active_markets(now):
            snap = self.d.hub.snapshot(tracked.definition.condition_id)
            if snap is None:
                continue
            out.append(
                {
                    "slug": snap.market.slug,
                    "time_to_expiry_s": snap.time_to_expiry_ms / 1000,
                    "quotes": [
                        {"outcome": q.outcome, "bid": q.best_bid, "ask": q.best_ask}
                        for q in snap.quotes
                    ],
                    "spot": snap.reference.spot,
                    "price_to_beat": snap.reference.price_to_beat,
                    "stale_reasons": list(snap.stale_reasons),
                }
            )
        return out

    def model_metrics(self) -> dict[str, Any]:
        preds = self.stats.predictions
        n = len(preds)
        brier = sum((p - y) ** 2 for p, _, y in preds) / n if n else None
        market_brier = sum((m - y) ** 2 for _, m, y in preds) / n if n else None
        hit = sum(y for _, _, y in preds) / n if n else None
        mean_p = sum(p for p, _, _ in preds) / n if n else None
        return {
            "settled_predictions": n,
            "brier": brier,
            "market_brier": market_brier,
            "hit_rate": hit,
            "mean_predicted": mean_p,
            "reject_reasons": dict(sorted(self.stats.reject_reasons.items())[:30]),
            "candidates_passing": self.stats.candidates_passing,
            "entries_submitted": self.stats.entries_submitted,
            "exits_submitted": self.stats.exits_submitted,
            "llm_blocked": self.stats.llm_blocked,
        }
