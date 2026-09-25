"""Resolution-source reference prices from Polymarket RTDS.

Topics (docs/research.md §4): ``crypto_prices_chainlink`` (Chainlink spot),
``crypto_prices_twap_sixty`` (Chainlink 60 s TWAP — the BTC 5m settlement
source), ``crypto_prices`` (Binance, secondary, dispersion checks only).

Only information with receive time <= "now" is ever stored, so every query is
automatically free of lookahead.
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

E18 = Decimal(10) ** 18
MAX_MESSAGE_KEYS = 32
TOPIC_SPOT = "crypto_prices_chainlink"
TOPIC_TWAP60 = "crypto_prices_twap_sixty"
TOPIC_TWAP30 = "crypto_prices_twap_thirty"
TOPIC_SECONDARY = "crypto_prices"


@dataclass(frozen=True, slots=True)
class Tick:
    observed_ms: int  # Chainlink observation time (payload.timestamp)
    received_ms: int  # local receive time
    value: Decimal


class PriceSeries:
    """Append-only (by observation time) bounded series."""

    def __init__(self, max_age_ms: int = 20 * 60_000) -> None:
        self._ticks: deque[Tick] = deque()
        self._max_age = max_age_ms

    def __len__(self) -> int:
        return len(self._ticks)

    def add(self, tick: Tick) -> bool:
        if self._ticks and tick.observed_ms <= self._ticks[-1].observed_ms:
            return False  # duplicate or out-of-order observation: ignore
        self._ticks.append(tick)
        while self._ticks and tick.observed_ms - self._ticks[0].observed_ms > self._max_age:
            self._ticks.popleft()
        return True

    def latest(self) -> Tick | None:
        return self._ticks[-1] if self._ticks else None

    def at_or_before(self, observed_ms: int) -> Tick | None:
        best: Tick | None = None
        for tick in reversed(self._ticks):
            if tick.observed_ms <= observed_ms:
                best = tick
                break
        return best

    def exact(self, observed_ms: int) -> Tick | None:
        for tick in reversed(self._ticks):
            if tick.observed_ms == observed_ms:
                return tick
            if tick.observed_ms < observed_ms:
                return None
        return None

    def window(self, start_ms: int, end_ms: int) -> list[Tick]:
        return [t for t in self._ticks if start_ms <= t.observed_ms <= end_ms]


@dataclass(frozen=True, slots=True)
class PriceToBeat:
    value: Decimal | None
    source: str
    verified: bool
    detail: str


class ReferencePriceState:
    def __init__(
        self,
        *,
        symbol: str = "btc/usd",
        secondary_symbol: str | None = "btcusdt",
        max_jump_bps: float = 300.0,
        suspect_hold_ms: int = 10_000,
        vol_halflife_s: float = 300.0,
    ) -> None:
        self.symbol = symbol
        self.secondary_symbol = secondary_symbol
        self.spot = PriceSeries()
        self.twap60 = PriceSeries()
        self.secondary = PriceSeries()
        self._max_jump = max_jump_bps
        self._suspect_hold = suspect_hold_ms
        self.suspect_until_ms = 0
        self.outliers = 0
        self.malformed = 0
        self.publisher_lags_ms: deque[int] = deque(maxlen=200)
        # Observability only: messages seen per "type|topic|symbol" (bounded).
        self.message_counts: dict[str, int] = {}
        # EWMA variance of 1-second log returns (per second)
        self._lambda = 0.5 ** (1.0 / vol_halflife_s)
        self._ewma_var: float | None = None
        self._vol_samples = 0
        self._last_second: int | None = None
        self._last_second_price: float | None = None

    # ------------------------------------------------------------------ ingestion
    def on_frame(self, text: str, received_ms: int) -> int:
        """Parse an RTDS text frame. Returns number of ticks accepted."""
        try:
            data = json.loads(text)
        except ValueError:
            self.malformed += 1
            return 0
        items = data if isinstance(data, list) else [data]
        return sum(self._on_message(item, received_ms) for item in items if isinstance(item, dict))

    def _on_message(self, msg: dict[str, Any], received_ms: int) -> int:
        topic = msg.get("topic")
        payload = msg.get("payload")
        self._count(msg, payload)
        if msg.get("type") != "update" or not isinstance(payload, dict):
            return 0
        try:
            symbol = str(payload.get("symbol", "")).lower()
            observed = int(payload["timestamp"])
            if "full_accuracy_value" in payload:
                value = Decimal(str(payload["full_accuracy_value"])) / E18
            else:
                value = Decimal(str(payload["value"]))
        except (KeyError, ValueError, TypeError, InvalidOperation):
            self.malformed += 1
            return 0
        if not value.is_finite() or value <= 0:
            self.malformed += 1
            return 0
        outer = msg.get("timestamp")
        if isinstance(outer, int):
            self.publisher_lags_ms.append(received_ms - outer)
        tick = Tick(observed, received_ms, value)
        if topic == TOPIC_SPOT and symbol == self.symbol:
            if not self._plausible(self.spot.latest(), tick, received_ms):
                return 0
            if self.spot.add(tick):
                self._update_vol(tick)
                return 1
        elif topic == TOPIC_TWAP60 and symbol == self.symbol:
            if payload.get("window_s") not in (None, 60):
                self.malformed += 1
                return 0
            if not self._plausible(self.twap60.latest(), tick, received_ms):
                return 0
            return int(self.twap60.add(tick))
        elif topic == TOPIC_SECONDARY and self.secondary_symbol and symbol == self.secondary_symbol:
            return int(self.secondary.add(tick))
        return 0

    def _count(self, msg: dict[str, Any], payload: Any) -> None:
        symbol = str(payload.get("symbol", "")).lower() if isinstance(payload, dict) else "-"
        key = f"{msg.get('type')}|{msg.get('topic')}|{symbol}"[:80]
        if key in self.message_counts or len(self.message_counts) < MAX_MESSAGE_KEYS:
            self.message_counts[key] = self.message_counts.get(key, 0) + 1

    def _plausible(self, prev: Tick | None, tick: Tick, received_ms: int) -> bool:
        if prev is None:
            return True
        jump_bps = abs(math.log(float(tick.value) / float(prev.value))) * 1e4
        dt_s = max(1.0, (tick.observed_ms - prev.observed_ms) / 1000)
        if jump_bps > self._max_jump * max(1.0, math.sqrt(dt_s / 5)):
            self.outliers += 1
            self.suspect_until_ms = received_ms + self._suspect_hold
            return False
        return True

    def _update_vol(self, tick: Tick) -> None:
        second = tick.observed_ms // 1000
        price = float(tick.value)
        if self._last_second is None or self._last_second_price is None:
            self._last_second, self._last_second_price = second, price
            return
        if second <= self._last_second:
            self._last_second_price = price  # same second: keep the last print
            return
        steps = min(second - self._last_second, 600)
        r = math.log(price / self._last_second_price)
        # Spread the move evenly over the elapsed seconds (piecewise-constant path).
        per_step_var = (r * r) / steps
        for _ in range(steps):
            if self._ewma_var is None:
                self._ewma_var = per_step_var
            else:
                self._ewma_var = self._lambda * self._ewma_var + (1 - self._lambda) * per_step_var
            self._vol_samples += 1
        self._last_second, self._last_second_price = second, price

    # ------------------------------------------------------------------ queries
    def vol_per_sqrt_s(self) -> tuple[float | None, int]:
        """(sigma of log price per sqrt(second), number of 1s samples)."""
        if self._ewma_var is None:
            return None, self._vol_samples
        return math.sqrt(self._ewma_var), self._vol_samples

    def is_suspect(self, now_ms: int) -> bool:
        return now_ms < self.suspect_until_ms

    def dispersion_bps(self) -> float | None:
        a, b = self.spot.latest(), self.secondary.latest()
        if a is None or b is None:
            return None
        return abs(math.log(float(a.value) / float(b.value))) * 1e4

    def trailing_average(self, start_ms: int, end_ms: int) -> tuple[Decimal | None, float]:
        """Time-weighted average of spot over [start, end] (piecewise constant).

        Returns (average, coverage) where coverage is the fraction of the
        interval backed by observations. Used for the observed part of the
        settlement TWAP window.
        """
        if end_ms <= start_ms:
            return None, 0.0
        anchor = self.spot.at_or_before(start_ms)
        ticks = self.spot.window(start_ms + 1, end_ms)
        points = ([anchor] if anchor else []) + ticks
        if not points:
            return None, 0.0
        total = Decimal(0)
        covered = 0
        for i, tick in enumerate(points):
            seg_start = max(start_ms, tick.observed_ms)
            seg_end = points[i + 1].observed_ms if i + 1 < len(points) else end_ms
            seg_end = min(seg_end, end_ms)
            if seg_end > seg_start:
                total += tick.value * (seg_end - seg_start)
                covered += seg_end - seg_start
        if covered == 0:
            return None, 0.0
        return total / covered, covered / (end_ms - start_ms)

    def price_to_beat(
        self,
        window_start_ms: int,
        official: Decimal | None,
        *,
        tolerance_bps: float,
        accept_stream_only: bool = False,
    ) -> PriceToBeat:
        stream = self.twap60.exact(window_start_ms)
        if official is not None and stream is not None:
            diff = abs(float(official / stream.value) - 1) * 1e4
            if diff <= tolerance_bps:
                return PriceToBeat(official, "gamma+rtds", True, f"agree within {diff:.3f}bps")
            return PriceToBeat(None, "conflict", False, f"gamma vs rtds differ {diff:.2f}bps")
        if official is not None:
            return PriceToBeat(official, "gamma", True, "official eventMetadata.priceToBeat")
        if stream is not None:
            return PriceToBeat(
                stream.value, "rtds", accept_stream_only, "stream TWAP at window start only"
            )
        return PriceToBeat(None, "none", False, "price to beat unknown")
