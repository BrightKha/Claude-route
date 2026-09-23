"""Async runner for live public data (paper trading and pure recording).

Tasks (all stop on SIGINT/SIGTERM):

* two resilient WebSockets (CLOB market channel, RTDS) — every connection
  change is surfaced to the hub, which invalidates books and halts trading;
* Gamma discovery every ``discovery_interval_s`` (validates new markets,
  learns resolutions) and dynamic (un)subscription of token ids;
* REST book resync for any tracked book that is invalid;
* the decision loop (``TradingCore.step``) every ``decision_interval_ms``;
* the watchdog task and the loop-stall detector thread;
* the recorder: every raw message is persisted for replay.

This module never talks to an authenticated endpoint; see ``run_live``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from polymarket_bot.adapters.http_base import PublicDataError
from polymarket_bot.adapters.polymarket_public import ClobPublicData, GammaDiscovery
from polymarket_bot.adapters.ws_client import ResilientWebSocket
from polymarket_bot.app.build import Assembly, assemble
from polymarket_bot.config.app_config import AppConfig, MarketDataConfig
from polymarket_bot.data.recorder import SessionRecorder
from polymarket_bot.domain.clock import Clock, SystemClock
from polymarket_bot.domain.types import TradingMode
from polymarket_bot.llm.reviewer import ReviewClient
from polymarket_bot.market.clob_messages import build_market_subscribe, build_market_update
from polymarket_bot.market.discovery import refresh_markets
from polymarket_bot.market.hub import MarketDataHub
from polymarket_bot.ports import RawMessage
from polymarket_bot.watchdog.watchdog import LoopStallDetector

log = logging.getLogger(__name__)

RESYNC_CHECK_S = 5.0
HEARTBEAT_S = 0.5


def rtds_subscribe_frame(md: MarketDataConfig) -> str:
    """RTDS subscription (docs/research.md §4)."""
    topics = [
        ("crypto_prices_chainlink", md.reference_symbol),
        ("crypto_prices_twap_sixty", md.reference_symbol),
    ]
    if md.secondary_symbol:
        topics.append(("crypto_prices", md.secondary_symbol))
    subs = [
        {"topic": t, "type": "update", "filters": json.dumps({"symbol": s}, separators=(",", ":"))}
        for t, s in topics
    ]
    return json.dumps({"action": "subscribe", "subscriptions": subs}, separators=(",", ":"))


class LiveDataFeeds:
    def __init__(
        self,
        config: AppConfig,
        clock: Clock,
        hub: MarketDataHub,
        on_message: Callable[[RawMessage], list[str]],
        recorder: SessionRecorder,
    ) -> None:
        md = config.market_data
        self._cfg = config
        self._clock = clock
        self._hub = hub
        self._on_message = on_message
        self._recorder = recorder
        self._subscribed: set[str] = set()
        common: dict[str, Any] = {
            "clock": clock,
            "reconnect_base_s": md.reconnect_base_s,
            "reconnect_max_s": md.reconnect_max_s,
            "jitter": md.reconnect_jitter,
        }
        self.market_ws = ResilientWebSocket(
            url=md.clob_market_ws_url,
            source="clob_ws",
            initial_frames=self._market_frames,
            ping_interval_s=md.clob_ping_interval_s,
            silence_timeout_s=md.clob_silence_timeout_s,
            **common,
        )
        self.rtds_ws = ResilientWebSocket(
            url=md.rtds_ws_url,
            source="rtds",
            initial_frames=lambda: [rtds_subscribe_frame(md)],
            ping_interval_s=md.rtds_ping_interval_s,
            silence_timeout_s=md.rtds_silence_timeout_s,
            **common,
        )
        self.gamma = GammaDiscovery(md, clock)
        self.clob = ClobPublicData(md, clock)

    def _market_frames(self) -> list[str]:
        tokens = sorted(self._subscribed)
        return [build_market_subscribe(tokens)] if tokens else []

    async def _handle(self, msg: RawMessage) -> None:
        self._recorder.record_raw(msg)
        new = self._on_message(msg)
        if new:
            await self._subscribe(new)

    async def _subscribe(self, tokens: list[str]) -> None:
        fresh = [t for t in tokens if t not in self._subscribed]
        if not fresh:
            return
        first = not self._subscribed
        self._subscribed.update(fresh)
        if first:
            # First subscription on this connection: send the full subscribe message
            # (reconnects replay it through ``_market_frames``).
            await self.market_ws.send(build_market_subscribe(sorted(self._subscribed)))
        else:
            await self.market_ws.send(build_market_update(fresh, subscribe=True))

    async def pump(self, ws: ResilientWebSocket) -> None:
        async for msg in ws.stream():
            await self._handle(msg)

    async def discovery_loop(self, stop: asyncio.Event) -> None:
        md = self._cfg.market_data
        while not stop.is_set():
            try:
                msg, new = await refresh_markets(self.gamma, self._hub, md, self._clock.now_ms())
                self._recorder.record_raw(msg)
                if new:
                    await self._subscribe(new)
                await self._unsubscribe_stale()
            except (PublicDataError, ValueError) as exc:
                log.warning("discovery failed: %s", exc)
            await _sleep_or_stop(stop, md.discovery_interval_s)

    async def _unsubscribe_stale(self) -> None:
        tracked = set(self._hub.tracked_tokens(self._clock.now_ms()))
        stale = sorted(self._subscribed - tracked)
        if stale:
            self._subscribed.difference_update(stale)
            await self.market_ws.send(build_market_update(stale, subscribe=False))

    async def resync_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            now = self._clock.now_ms()
            for token in self._hub.tracked_tokens(now):
                book = self._hub.books.get(token)
                if book is None or book.valid or not self.market_ws.connected:
                    continue
                try:
                    await self._handle(await self.clob.fetch_book(token))
                except PublicDataError as exc:
                    log.warning("REST book resync failed for %s…: %s", token[:10], exc)
            await _sleep_or_stop(stop, RESYNC_CHECK_S)

    async def close(self) -> None:
        await self.market_ws.close()
        await self.rtds_ws.close()
        await self.gamma.aclose()
        await self.clob.aclose()


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def _review_client(config: AppConfig) -> ReviewClient | None:
    if config.llm.mode == "off":
        return None
    try:
        from polymarket_bot.llm.claude_client import ClaudeReviewClient  # noqa: PLC0415
    except ImportError:
        log.warning("anthropic SDK not installed (extra 'llm'); Claude review unavailable")
        return None
    client = ClaudeReviewClient(config.llm)
    if not client.available:
        log.warning("ANTHROPIC_API_KEY not set; Claude review unavailable")
    return client


def _install_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def run_paper(config: AppConfig, data_dir: Path, *, trade: bool) -> None:
    """Paper trading (``trade=True``) or pure recording on live public data."""
    clock = SystemClock()
    recorder = SessionRecorder(data_dir / "recordings", session_name(trade=trade), clock)
    asm = assemble(
        config,
        mode=TradingMode.PAPER,
        clock=clock,
        data_dir=data_dir,
        review_client=_review_client(config) if trade else None,
        recorder=recorder,
        with_inbox=trade,
    )
    on_message = asm.core.on_message if trade else asm.hub.on_raw
    feeds = LiveDataFeeds(config, clock, asm.hub, on_message, recorder)
    if trade:
        asm.core.start()
    await run_session(asm, feeds, config, data_dir, recorder, trade=trade)


def session_name(*, trade: bool, live: bool = False) -> str:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
    kind = "live" if live else "paper" if trade else "record"
    return f"{kind}-{stamp}"


async def run_session(
    asm: Assembly,
    feeds: LiveDataFeeds,
    config: AppConfig,
    data_dir: Path,
    recorder: SessionRecorder,
    *,
    trade: bool,
    stop: asyncio.Event | None = None,
    already_running: list[asyncio.Task[None]] | None = None,
) -> None:
    """Run feeds, decision loop, watchdog and loop-stall detector until stopped."""
    core = asm.core
    stop = stop or asyncio.Event()
    _install_signals(stop)
    stall = LoopStallDetector(
        asm.health, asm.state, config.watchdog.loop_stall_s, data_dir / "incidents"
    )
    stall.start()

    async def decision_loop() -> None:
        interval = config.strategy.decision_interval_ms / 1000
        while not stop.is_set():
            await core.step()
            await _sleep_or_stop(stop, interval)

    async def heartbeat_loop() -> None:
        while not stop.is_set():
            asm.health.beat_loop()
            await _sleep_or_stop(stop, HEARTBEAT_S)

    tasks = list(already_running or [])
    jobs: list[Coroutine[Any, Any, None]] = []
    if not tasks:
        jobs += [
            feeds.pump(feeds.market_ws),
            feeds.pump(feeds.rtds_ws),
            feeds.discovery_loop(stop),
            feeds.resync_loop(stop),
        ]
    jobs += [decision_loop(), asm.watchdog.run(stop)] if trade else [heartbeat_loop()]
    tasks += [asyncio.create_task(j) for j in jobs]
    log.info("session started (data dir %s, trading=%s)", data_dir, trade)
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if trade:
            await core.shutdown()
        await feeds.close()
        stall.stop()
        recorder.close()
        log.info("session stopped")


async def run_live(config: AppConfig, data_dir: Path) -> int:
    """Live trading entry point. See ``app/live.py``; reached only via the CLI live lock."""
    from polymarket_bot.app.live import run_live_session  # noqa: PLC0415

    return await run_live_session(config, data_dir)
