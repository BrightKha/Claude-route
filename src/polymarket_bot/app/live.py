"""Live session bootstrap. Reached only through ``cli --mode live`` after the
static live-lock checks passed. NOT VERIFIED end-to-end (docs/progress.md).

Startup sequence (fail closed at every step):

1. connect the live venue (no implicit wallet deployment, no approvals);
2. reconcile: the account snapshot must be complete, with **no positions and no
   open orders** — the bot only trades a dedicated, flat wallet it fully owns;
3. start the public data feeds and wait for a fresh market snapshot;
4. re-evaluate the full live lock with the runtime facts (reconciliation OK,
   market data healthy) — only then is a LiveAuthorization minted;
5. start the core (SYNCING -> LIVE) and run the same session loop as paper.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from polymarket_bot.app.build import assemble
from polymarket_bot.app.runner import (
    LiveDataFeeds,
    _review_client,
    run_session,
    session_name,
)
from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.config.risk_policy import clamp_to_hard_caps, policy_hash
from polymarket_bot.config.settings import load_env_settings
from polymarket_bot.data.recorder import SessionRecorder
from polymarket_bot.domain.clock import SystemClock
from polymarket_bot.domain.types import TradingMode
from polymarket_bot.lifecycle.state_machine import LiveAuthorization
from polymarket_bot.market.hub import MarketDataHub
from polymarket_bot.promotion.gates import Evidence, evaluate_promotion
from polymarket_bot.promotion.live_lock import evaluate_live_lock
from polymarket_bot.security.compliance import compliance_gate
from polymarket_bot.security.secrets import secret_env_vars_present
from polymarket_bot.storage.sqlite_store import StateStore

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_LOCKED = 2
MARKET_DATA_WAIT_S = 180.0


async def run_live_session(config: AppConfig, data_dir: Path) -> int:
    from polymarket_bot.adapters.polymarket_live import PolymarketLiveVenue  # noqa: PLC0415

    clock = SystemClock()
    try:
        venue = await PolymarketLiveVenue.connect(clock)
    except Exception as exc:
        log.critical("live venue connection refused: %s", type(exc).__name__)
        return EXIT_FAIL
    snapshot = await venue.account_snapshot()
    problems = []
    if not snapshot.complete:
        problems.append("account snapshot incomplete")
    if snapshot.positions:
        problems.append(f"wallet holds {len(snapshot.positions)} positions (must start flat)")
    if snapshot.open_orders:
        problems.append(f"wallet has {len(snapshot.open_orders)} open orders")
    if problems:
        log.critical("live start refused: %s", "; ".join(problems))
        await venue.close()
        return EXIT_LOCKED

    recorder = SessionRecorder(data_dir / "recordings", session_name(trade=True, live=True), clock)
    asm = assemble(
        config,
        mode=TradingMode.LIVE,
        clock=clock,
        data_dir=data_dir,
        review_client=_review_client(config),
        recorder=recorder,
        with_inbox=True,
        live_venue=venue,
        live_cash_usd=snapshot.collateral_usd,
    )
    feeds = LiveDataFeeds(config, clock, asm.hub, asm.core.on_message, recorder)
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(feeds.pump(feeds.market_ws)),
        asyncio.create_task(feeds.pump(feeds.rtds_ws)),
        asyncio.create_task(feeds.discovery_loop(stop)),
        asyncio.create_task(feeds.resync_loop(stop)),
    ]
    healthy = await _wait_for_market_data(asm.hub, clock, MARKET_DATA_WAIT_S)
    auth = await _mint_authorization(config, data_dir, market_data_ok=healthy) if healthy else None
    if auth is None:
        log.critical("live lock not satisfied at runtime; staying locked")
        stop.set()
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        await feeds.close()
        recorder.close()
        await venue.close()
        return EXIT_LOCKED
    asm.core.d.live_authorization = auth
    asm.core.start()
    try:
        await run_session(
            asm, feeds, config, data_dir, recorder, trade=True, stop=stop, already_running=tasks
        )
    finally:
        await venue.close()
    return EXIT_OK


async def _wait_for_market_data(hub: MarketDataHub, clock: SystemClock, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for tracked in hub.active_markets(clock.now_ms()):
            snap = hub.snapshot(tracked.definition.condition_id)
            if snap is not None and snap.is_fresh:
                return True
        await asyncio.sleep(1.0)
    return False


async def _mint_authorization(
    config: AppConfig, data_dir: Path, *, market_data_ok: bool
) -> LiveAuthorization | None:
    env = load_env_settings()
    small_live = config.promotion_stage_required_for_live == "SMALL_LIVE"
    policy, notes = clamp_to_hard_caps(config.risk, small_live=small_live)
    phash = policy_hash(policy)
    store = StateStore(data_dir / "state.sqlite", read_only=True)
    events = store.promotion_events()
    evidence = [
        Evidence(
            kind=str(e["body"].get("kind")),
            strategy_version=str(e["body"].get("strategy_version")),
            body=e["body"],
            ts_ms=int(e["ts_ms"]),
        )
        for e in events
        if e["action"] == "evidence"
    ]
    approvals = [e["body"] for e in events if e["action"] == "approve"]
    promotion = evaluate_promotion(
        evidence, approvals, strategy_version=config.strategy.version, policy_hash=phash
    )
    engaged = store.kill_switch_state()[0] or (data_dir / "KILL_SWITCH").exists()
    checks, auth = evaluate_live_lock(
        env=env,
        config=config,
        policy_hash=phash,
        hard_cap_clamps=notes,
        promotion=promotion,
        compliance=await compliance_gate(config.compliance, env.operator_jurisdiction),
        kill_switch_engaged=engaged,
        reconciliation_ok=True,  # established by the flat-wallet snapshot above
        market_data_ok=market_data_ok,
        credential_names_present=secret_env_vars_present(),
        now_ms=int(time.time() * 1000),
    )
    for c in checks:
        if not c.passed:
            log.critical("live lock check failed: %s — %s", c.name, c.detail)
    return auth
