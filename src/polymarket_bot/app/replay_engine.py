"""Replay a recorded session through the production pipeline (no lookahead).

For every recorded message ``e_k`` received at ``t_k``:

1. run every decision tick ``tau < t_k`` (clock advanced to ``tau``) — ticks only
   see messages received before them;
2. advance the clock to ``t_k`` and let the paper exchange match orders due by
   ``t_k`` against the book *before* ``e_k`` is applied;
3. apply ``e_k`` through the same normalisation code as live.

The watchdog is evaluated on every tick, like the live watchdog task. Claude is
not called in replay (``reviewer=None``): results are "without Claude".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from polymarket_bot.app.build import Assembly, assemble
from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.data.replay import SessionInfo, iter_messages, open_session
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.types import TradingMode
from polymarket_bot.ports import RawMessage

log = logging.getLogger(__name__)

DEFAULT_TAIL_MS = 60_000


@dataclass
class ReplayRun:
    session: SessionInfo
    assembly: Assembly
    messages: int
    first_ms: int
    last_ms: int

    @property
    def synthetic(self) -> bool:
        return self.session.synthetic


async def run_replay(
    config: AppConfig,
    session_path: Path,
    out_dir: Path,
    *,
    tail_ms: int = DEFAULT_TAIL_MS,
    publish_interval_ms: int = 60_000,
) -> ReplayRun:
    session = open_session(session_path)
    messages = iter_messages(session)
    first = next(messages, None)
    if first is None:
        raise ValueError(f"empty session {session_path}")
    clock = SimulatedClock(first.received_ms)
    asm = assemble(
        config,
        mode=TradingMode.REPLAY,
        clock=clock,
        data_dir=out_dir,
        fsync_audit=False,
        publish_interval_ms=publish_interval_ms,
        evidence_source=f"replay:{session.path.name}",
        synthetic=session.synthetic,
    )
    core = asm.core
    paper = asm.paper
    assert paper is not None
    core.start()
    interval = config.strategy.decision_interval_ms
    next_tick = first.received_ms + interval
    count = 0

    async def tick(t: int) -> None:
        clock.advance_to(t)
        await asm.watchdog.check_once()
        await core.step()

    def apply(msg: RawMessage) -> None:
        clock.advance_to(msg.received_ms)
        paper.advance(msg.received_ms)
        core.on_message(msg)

    apply(first)
    count += 1
    last_ms = first.received_ms
    for msg in messages:
        while next_tick < msg.received_ms:
            await tick(next_tick)
            next_tick += interval
        apply(msg)
        count += 1
        last_ms = msg.received_ms
    end = last_ms + tail_ms
    while next_tick <= end:
        await tick(next_tick)
        next_tick += interval
    await core.shutdown()
    log.info("replay done: %d messages, %d steps", count, core.stats.steps)
    return ReplayRun(session, asm, count, first.received_ms, last_ms)
