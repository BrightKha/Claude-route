"""Clock drift estimation from exchange timestamps.

``/time`` has 1-second resolution (VERIFIED), too coarse for sub-second checks.
We therefore use millisecond exchange timestamps on CLOB WS events: the median
of ``receive - exchange`` over recent events equals clock offset + network
latency. A negative median means our clock is behind the exchange; a large
positive one means we are ahead or the link is degraded. Both are unsafe.
"""

from __future__ import annotations

import statistics
from collections import deque


class ClockDriftEstimator:
    def __init__(self, window: int = 200, min_samples: int = 20) -> None:
        self._lags: deque[int] = deque(maxlen=window)
        self._min = min_samples
        self.coarse_offset_ms: int | None = None

    def add(self, exchange_ms: int, received_ms: int) -> None:
        self._lags.append(received_ms - exchange_ms)

    def add_server_time(self, server_ms: int, local_before_ms: int, local_after_ms: int) -> None:
        """Coarse check with /time (seconds): offset within +/- 1 s + RTT/2."""
        midpoint = (local_before_ms + local_after_ms) // 2
        self.coarse_offset_ms = midpoint - server_ms

    def estimate_ms(self) -> int | None:
        if len(self._lags) < self._min:
            return None
        return int(statistics.median(self._lags))

    def reset(self) -> None:
        self._lags.clear()
