"""Submission-rate bookkeeping for the Risk Engine (duplicate/cooldown/rate checks)."""

from __future__ import annotations

from collections import deque

from polymarket_bot.domain.types import OrderPurpose
from polymarket_bot.risk.engine import RateView

MINUTE = 60_000
HOUR = 3_600_000
DAY = 86_400_000


class RateTracker:
    def __init__(self) -> None:
        self._entries: deque[int] = deque()
        self._exits: deque[int] = deque()
        self._last_by_market: dict[str, int] = {}

    def record(self, ts_ms: int, condition_id: str, purpose: OrderPurpose) -> None:
        (self._entries if purpose is OrderPurpose.ENTRY else self._exits).append(ts_ms)
        self._last_by_market[condition_id] = ts_ms

    def view(self, now_ms: int) -> RateView:
        while self._entries and now_ms - self._entries[0] > DAY:
            self._entries.popleft()
        while self._exits and now_ms - self._exits[0] > MINUTE:
            self._exits.popleft()
        return RateView(
            submissions_last_minute=sum(1 for t in self._entries if now_ms - t <= MINUTE),
            submissions_last_hour=sum(1 for t in self._entries if now_ms - t <= HOUR),
            submissions_last_day=len(self._entries),
            exits_last_minute=len(self._exits),
            last_submit_ms_by_market=dict(self._last_by_market),
        )
