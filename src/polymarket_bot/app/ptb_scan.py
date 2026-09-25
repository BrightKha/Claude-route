"""Offline price-to-beat evidence from recorded sessions (``ptb-validate``).

Replays the Gamma and RTDS messages of each *real* recording through a fresh
:class:`MarketDataHub` and collects, per window, the official price to beat
(once Gamma published it, after the window) and the RTDS 60 s TWAP tick at the
window start. SYNTHETIC sessions are skipped: they are never evidence.

Recorded messages from concurrent tasks can be a few ms out of order, which
the strict replay reader rejects; this scan only needs coarse ordering (the
official value arrives minutes after the stream tick), so the simulated clock
simply never moves backwards.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.data.replay import SessionInfo, open_session
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.market.hub import MarketDataHub
from polymarket_bot.ports import RawMessage
from polymarket_bot.strategies.btc_5m.ptb_validation import PriceToBeatValidator
from polymarket_bot.watchdog.health import HealthRegistry

SCANNED_SOURCES = frozenset({"gamma", "rtds"})


@dataclass(frozen=True, slots=True)
class SessionScan:
    session: str
    synthetic: bool
    messages: int
    observations: list[dict[str, Any]]
    error: str | None = None


def find_sessions(root: Path) -> list[Path]:
    if (root / "session.json").exists():
        return [root]
    return sorted(p.parent for p in root.glob("*/session.json"))


def _messages(session: SessionInfo) -> Iterator[RawMessage]:
    for part in session.parts:
        with part.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                env = json.loads(line)
                if env.get("src") in SCANNED_SOURCES:
                    yield RawMessage(
                        source=str(env["src"]),
                        kind=str(env["kind"]),
                        payload=env.get("data"),
                        received_ms=int(env["t"]),
                        monotonic_ns=int(env.get("mono", 0)),
                        meta=env.get("meta"),
                    )


def scan_session(config: AppConfig, path: Path) -> SessionScan:
    session = open_session(path)
    if session.synthetic:
        return SessionScan(path.name, True, 0, [], "synthetic session skipped (not evidence)")
    clock = SimulatedClock(0)
    hub = MarketDataHub(config, clock, HealthRegistry())
    validator = PriceToBeatValidator(
        config.fair_value, source=f"recording:{path.name}", synthetic=False
    )
    hub.ptb_validator = validator
    count = 0
    try:
        for msg in _messages(session):
            clock.advance_to(max(clock.now_ms(), msg.received_ms))
            hub.on_raw(msg)
            count += 1
    except (ValueError, KeyError, TypeError) as exc:
        return SessionScan(path.name, False, count, validator.observations, repr(exc)[:200])
    return SessionScan(path.name, False, count, validator.observations)
