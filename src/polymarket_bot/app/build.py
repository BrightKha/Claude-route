"""Assemble a :class:`TradingCore` and its safety components for one mode.

Paper, replay and live share this exact assembly; live only swaps the venue
(``live_venue``) and additionally needs a LiveAuthorization before the core can
enter LIVE (``app/live.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from polymarket_bot.adapters.paper_exchange import PaperExchange
from polymarket_bot.app.core import CoreDeps, TradingCore
from polymarket_bot.audit.audit_log import AuditLog
from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.config.risk_policy import clamp_to_hard_caps, policy_hash
from polymarket_bot.data.recorder import SessionRecorder
from polymarket_bot.domain.clock import Clock
from polymarket_bot.domain.market import OrderBookSnapshot
from polymarket_bot.domain.types import TradingMode
from polymarket_bot.lifecycle.kill_switch import KillSwitch
from polymarket_bot.lifecycle.state_machine import BotStateMachine, StateChange
from polymarket_bot.llm.budget import LLMBudget, day_start_ms
from polymarket_bot.llm.reviewer import CandidateReviewer, ReviewClient
from polymarket_bot.market.hub import MarketDataHub
from polymarket_bot.ports import AccountProvider, TradingProvider
from polymarket_bot.risk.engine import RiskEngine
from polymarket_bot.storage.sqlite_store import ProposalInbox, StateStore
from polymarket_bot.strategies.btc_5m.fair_value import FairValueEngine, LogisticCalibrator
from polymarket_bot.watchdog.health import HealthRegistry
from polymarket_bot.watchdog.watchdog import Watchdog

log = logging.getLogger(__name__)


@dataclass
class Assembly:
    core: TradingCore
    paper: PaperExchange | None  # None in live mode
    watchdog: Watchdog
    store: StateStore
    audit: AuditLog
    state: BotStateMachine
    kill_switch: KillSwitch
    hub: MarketDataHub
    health: HealthRegistry
    policy_notes: list[str]


def assemble(
    config: AppConfig,
    *,
    mode: TradingMode,
    clock: Clock,
    data_dir: Path,
    review_client: ReviewClient | None = None,
    recorder: SessionRecorder | None = None,
    with_inbox: bool = False,
    fsync_audit: bool = True,
    publish_interval_ms: int = 2_000,
    live_venue: Any = None,
    live_cash_usd: Decimal | None = None,
) -> Assembly:
    if (mode is TradingMode.LIVE) != (live_venue is not None) or mode is TradingMode.DISABLED:
        raise ValueError(f"invalid assembly for mode {mode}")
    if mode is TradingMode.LIVE and live_cash_usd is None:
        raise ValueError("live assembly needs the reconciled starting collateral")
    data_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(data_dir / "state.sqlite")
    audit = AuditLog(data_dir / "audit.jsonl", clock, fsync=fsync_audit)
    health = HealthRegistry()
    state = BotStateMachine(clock)

    def persist(change: StateChange) -> None:
        store.save_bot_state(
            state=change.to_state.value,
            reason=change.reason,
            manual_only=change.manual_only,
            ts_ms=change.ts_ms,
            from_state=change.from_state.value,
        )
        audit.append("state_change", {"change": change})
        if recorder is not None:
            recorder.record_bot("state_change", change)

    state.subscribe(persist)
    kill_switch = KillSwitch(data_dir, store, state, audit, clock)
    small_live = (
        mode is TradingMode.LIVE and config.promotion_stage_required_for_live == "SMALL_LIVE"
    )
    policy, notes = clamp_to_hard_caps(config.risk, small_live=small_live)
    for note in notes:
        log.warning("risk policy: %s", note)
    risk = RiskEngine(policy, policy_hash(policy), clock)
    hub = MarketDataHub(config, clock, health)

    def book_source(token: str) -> OrderBookSnapshot | None:
        book = hub.books.get(token)
        return book.snapshot() if book else None

    paper = (
        None
        if mode is TradingMode.LIVE
        else PaperExchange(config.paper, clock, book_source, source=mode.value)
    )
    venue: Any = live_venue if live_venue is not None else paper
    trading: TradingProvider = venue
    account: AccountProvider = venue

    async def cancel_all() -> bool:
        return (await trading.cancel_all()).ok

    def write_incident(severity: str, kind: str, body: dict[str, object]) -> None:
        store.insert_incident(clock.now_ms(), severity, kind, body)

    watchdog = Watchdog(
        config.watchdog,
        health,
        state,
        clock,
        cancel_all=cancel_all,
        write_incident=write_incident,
        engage_kill_switch=lambda reason: kill_switch.engage(reason, source="watchdog"),
    )
    calibrator = (
        LogisticCalibrator.load(Path(config.fair_value.calibrator_path))
        if config.fair_value.calibrator_path
        else None
    )
    reviewer: CandidateReviewer | None = None
    # Paper always gets a reviewer when the LLM is enabled, even without a usable client:
    # it then answers "not reviewed", which blocks trading in ``required`` mode (fail
    # closed). Replay has no reviewer: its results are explicitly "without Claude".
    if mode in (TradingMode.PAPER, TradingMode.LIVE) and config.llm.mode != "off":
        now = clock.now_ms()
        budget = LLMBudget(
            config.llm, spent_today_usd=store.llm_spend_since(day_start_ms(now)), now_ms=now
        )
        reviewer = CandidateReviewer(
            config.llm, review_client, budget, store=store, audit=audit, clock=clock
        )
    deps = CoreDeps(
        config=config,
        mode=mode,
        clock=clock,
        store=store,
        audit=audit,
        health=health,
        state=state,
        kill_switch=kill_switch,
        hub=hub,
        trading=trading,
        account=account,
        risk=risk,
        fair_value=FairValueEngine(config.fair_value, calibrator),
        watchdog=watchdog,
        initial_cash_usd=live_cash_usd
        if live_cash_usd is not None
        else Decimal(config.paper.initial_balance_usd),
        reviewer=reviewer,
        inbox=ProposalInbox(data_dir / "inbox.sqlite") if with_inbox else None,
        recorder=recorder,
        settle_venue=paper,  # None in live: redemption happens on-chain, outside the bot
        publish_interval_ms=publish_interval_ms,
    )
    return Assembly(
        core=TradingCore(deps),
        paper=paper,
        watchdog=watchdog,
        store=store,
        audit=audit,
        state=state,
        kill_switch=kill_switch,
        hub=hub,
        health=health,
        policy_notes=notes,
    )
