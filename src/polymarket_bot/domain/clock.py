"""Clock abstraction.

Every component reads time through a :class:`Clock` so that replay/backtests
run on a simulated clock driven by recorded receive timestamps. This is the
basis of the no-lookahead guarantee: in replay, "now" is exactly the receive
time of the event being processed.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int:
        """Wall-clock UTC time in epoch milliseconds."""
        ...

    def monotonic_ns(self) -> int:
        """Monotonic time in nanoseconds (never goes backwards)."""
        ...


class SystemClock:
    """Real clock used for live and paper trading."""

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class SimulatedClock:
    """Clock advanced explicitly by the replay engine.

    Time can only move forward; attempting to go backwards raises, which
    surfaces out-of-order recordings instead of silently corrupting state.
    """

    def __init__(self, start_ms: int) -> None:
        self._now_ms = start_ms
        self._mono_ns = 0

    def now_ms(self) -> int:
        return self._now_ms

    def monotonic_ns(self) -> int:
        return self._mono_ns

    def advance_to(self, t_ms: int) -> None:
        if t_ms < self._now_ms:
            raise ValueError(f"simulated clock cannot go backwards: {t_ms} < {self._now_ms}")
        self._mono_ns += (t_ms - self._now_ms) * 1_000_000
        self._now_ms = t_ms


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def datetime_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime is not allowed; use timezone-aware UTC")
    return int(dt.timestamp() * 1000)
