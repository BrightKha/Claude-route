"""Resolution-source reference prices from Polymarket RTDS (legacy) and PolyBolt.

Per-topic wire contract (docs/research.md §4; VERIFIED against the official SDK
``polymarket-client`` 0.10.0/0.11.0 source, the official RTDS→PolyBolt
migration guide, and verbatim frames captured on the legacy host):

===========================  =========  ===================  =====  ========================
legacy topic                 series     source               quote  ``full_accuracy_value``
===========================  =========  ===================  =====  ========================
``crypto_prices_chainlink``  spot       Chainlink            USD    E18 fixed-point integer
``crypto_prices_twap_sixty`` twap60     Chainlink 60 s TWAP  USD    E18 fixed-point integer
``crypto_prices``            secondary  Binance              USDT   **plain decimal string**
===========================  =========  ===================  =====  ========================

The exact field is decoded with the encoding of *its topic* and is always
cross-checked against the float ``value`` of the same payload; a disagreement
is a unit error and the tick is rejected (``unit_mismatch``), never rescaled.
(Regression: every ``full_accuracy_value`` used to be divided by 1e18, so the
Binance price 84186.07 became 8.418607e-14 and the dispersion check rejected
every decision with 414462.93 bps.)

Every legacy subscription starts with a ``type: "subscribe"`` backfill
(``payload.data``: ~1 min Chainlink, ~2 min Binance); it seeds the series but is
never counted as live data. PolyBolt frames (``data/polybolt.py``) are applied
through :meth:`ReferencePriceState.apply_polybolt`.

Only information with receive time <= "now" is ever stored, so every query is
automatically free of lookahead.
"""

from __future__ import annotations

import json
import math
import re
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from polymarket_bot.data.polybolt import PolyBoltFrame

E18 = Decimal(10) ** 18
MAX_MESSAGE_KEYS = 32
TOPIC_SPOT = "crypto_prices_chainlink"
TOPIC_TWAP60 = "crypto_prices_twap_sixty"
TOPIC_TWAP30 = "crypto_prices_twap_thirty"
TOPIC_SECONDARY = "crypto_prices"
SPOT, TWAP60, SECONDARY = "spot", "twap60", "secondary"
SERIES = (SPOT, TWAP60, SECONDARY)
# |exact / value - 1| above this is a unit error. A float ``value`` carries ~1e-16
# relative precision; a wrong scale is off by >= 1e2 (any power of ten), so the
# bound only needs to separate those two regimes.
UNIT_CHECK_MAX_REL_DIFF = 1e-6
_INTEGER = re.compile(r"-?[0-9]+")
_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")

Encoding = Literal["e18", "decimal"]


@dataclass(frozen=True, slots=True)
class FeedContract:
    topic: str
    series: str
    source: str
    quote: str
    exact_encoding: Encoding  # encoding of ``full_accuracy_value`` on this topic
    window_s: int | None = None


LEGACY_CONTRACTS: dict[str, FeedContract] = {
    TOPIC_SPOT: FeedContract(TOPIC_SPOT, SPOT, "chainlink", "USD", "e18"),
    TOPIC_TWAP60: FeedContract(TOPIC_TWAP60, TWAP60, "chainlink-twap-60s", "USD", "e18", 60),
    TOPIC_SECONDARY: FeedContract(TOPIC_SECONDARY, SECONDARY, "binance", "USDT", "decimal"),
}


class UnitError(ValueError):
    """``full_accuracy_value`` and ``value`` disagree: wrong scale or encoding."""


@dataclass(frozen=True, slots=True)
class Tick:
    observed_ms: int  # source observation time (payload.timestamp)
    received_ms: int  # local receive time
    value: Decimal
    live: bool = True  # False for subscribe backfills (history)


@dataclass
class SeriesStats:
    updates: int = 0  # live update ticks accepted
    history_points: int = 0  # backfill ticks accepted
    rejected: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


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


def decode_exact(raw: object, encoding: Encoding) -> Decimal:
    """Decode ``full_accuracy_value`` with the encoding of its topic."""
    if not isinstance(raw, str):
        raise TypeError("full_accuracy_value must be a string")
    if encoding == "e18":
        if _INTEGER.fullmatch(raw) is None:
            raise ValueError("E18 full_accuracy_value must be an integer string")
        return Decimal(raw) / E18
    if _DECIMAL.fullmatch(raw) is None:
        raise ValueError("decimal full_accuracy_value must be a decimal string")
    return Decimal(raw)


def parse_point(point: dict[str, Any], encoding: Encoding) -> tuple[int, Decimal, str]:
    """(observed_ms, value, decoded_as) of one payload or backfill point."""
    observed = point["timestamp"]
    if isinstance(observed, str) and observed.isdigit():
        observed = int(observed)
    if isinstance(observed, bool) or not isinstance(observed, int):
        raise TypeError("timestamp must be integer epoch milliseconds")
    raw_value = point.get("value")
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float | str):
        raise TypeError("value must be a number")
    approx = Decimal(str(raw_value))
    if "full_accuracy_value" not in point:
        return observed, approx, "value"
    exact = decode_exact(point["full_accuracy_value"], encoding)
    if approx <= 0 or abs(float(exact) / float(approx) - 1) > UNIT_CHECK_MAX_REL_DIFF:
        raise UnitError(f"full_accuracy_value ({encoding}) {exact} vs value {approx}")
    return observed, exact, encoding


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
        self.symbol = symbol.lower()
        self.secondary_symbol = secondary_symbol.lower() if secondary_symbol else None
        self.spot = PriceSeries()
        self.twap60 = PriceSeries()
        self.secondary = PriceSeries()
        self._max_jump = max_jump_bps
        self._suspect_hold = suspect_hold_ms
        self.suspect_until_ms = 0
        self.outliers = 0
        self.publisher_lags_ms: deque[int] = deque(maxlen=200)
        # EWMA variance of 1-second log returns (per second)
        self._lambda = 0.5 ** (1.0 / vol_halflife_s)
        self._ewma_var: float | None = None
        self._vol_samples = 0
        self._last_second: int | None = None
        self._last_second_price: float | None = None
        # Observability (docs/diagnostics.md); never read by a decision.
        self.message_counts: dict[str, int] = {}
        self.series_stats: dict[str, SeriesStats] = {s: SeriesStats() for s in SERIES}
        self.frames = 0
        self.empty_frames = 0
        self.malformed_frames = 0
        self.server_errors = 0
        self.last_server_error: str | None = None
        self.last_live_ms: dict[str, int] = {}  # series -> receive time of last live tick
        self.last_raw: dict[str, dict[str, Any]] = {}  # series -> raw fields of last tick

    @property
    def malformed(self) -> int:
        """Frames or payloads that could not be decoded (all causes)."""
        rejected = sum(
            n
            for st in self.series_stats.values()
            for reason, n in st.rejected.items()
            if reason not in ("outlier", "out_of_order", "wrong_symbol")
        )
        return self.malformed_frames + rejected

    def _series(self, name: str) -> PriceSeries:
        return {SPOT: self.spot, TWAP60: self.twap60, SECONDARY: self.secondary}[name]

    # ------------------------------------------------------------------ ingestion
    def on_frame(self, text: str, received_ms: int) -> int:
        """Parse a legacy RTDS text frame. Returns number of live ticks accepted."""
        self.frames += 1
        if not text.strip():
            self.empty_frames += 1  # RTDS sends one empty frame at connect
            return 0
        try:
            data = json.loads(text)
        except ValueError:
            self.malformed_frames += 1
            return 0
        items = data if isinstance(data, list) else [data]
        return sum(self._on_message(item, received_ms) for item in items if isinstance(item, dict))

    def _count(self, msg: dict[str, Any], payload: Any) -> None:
        symbol = str(payload.get("symbol", "")).lower() if isinstance(payload, dict) else "-"
        key = f"{msg.get('type')}|{msg.get('topic')}|{symbol}"[:80]
        if key in self.message_counts or len(self.message_counts) < MAX_MESSAGE_KEYS:
            self.message_counts[key] = self.message_counts.get(key, 0) + 1

    def _on_message(self, msg: dict[str, Any], received_ms: int) -> int:
        if "statusCode" in msg and "topic" not in msg:
            self.server_errors += 1  # e.g. a rejected subscription batch
            self.last_server_error = str(msg.get("body"))[:200]
            return 0
        payload = msg.get("payload")
        self._count(msg, payload)
        contract = LEGACY_CONTRACTS.get(str(msg.get("topic")))
        if contract is None or not isinstance(payload, dict):
            return 0
        stats = self.series_stats[contract.series]
        if not isinstance(payload.get("symbol"), str):
            stats.reject("malformed")
            return 0
        wanted = self.secondary_symbol if contract.series == SECONDARY else self.symbol
        if wanted is None or str(payload["symbol"]).lower() != wanted:
            stats.reject("wrong_symbol")
            return 0
        if contract.window_s is not None and payload.get("window_s") not in (
            None,
            contract.window_s,
        ):
            stats.reject("window")
            return 0
        kind = msg.get("type")
        if kind == "subscribe":
            self._apply_backfill(contract, payload, received_ms)
            return 0
        if kind != "update":
            return 0
        outer = msg.get("timestamp")
        if isinstance(outer, int):
            self.publisher_lags_ms.append(received_ms - outer)
        try:
            observed, value, decoded_as = parse_point(payload, contract.exact_encoding)
        except UnitError:
            stats.reject("unit_mismatch")
            self.last_raw[f"{contract.series}_rejected"] = _raw_fields(payload, "unit_mismatch")
            return 0
        except (KeyError, ValueError, TypeError, InvalidOperation):
            stats.reject("malformed")
            return 0
        tick = Tick(observed, received_ms, value)
        if not self._accept(contract.series, tick, stats):
            return 0
        self.last_raw[contract.series] = _raw_fields(payload, decoded_as)
        return 1

    def _apply_backfill(
        self, contract: FeedContract, payload: dict[str, Any], received_ms: int
    ) -> None:
        points = payload.get("data")
        stats = self.series_stats[contract.series]
        if not isinstance(points, list):
            stats.reject("malformed")
            return
        for point in points:
            try:
                if not isinstance(point, dict):
                    raise TypeError("backfill point must be an object")
                observed, value, _ = parse_point(point, contract.exact_encoding)
            except UnitError:
                stats.reject("unit_mismatch")
                continue
            except (KeyError, ValueError, TypeError, InvalidOperation):
                stats.reject("malformed")
                continue
            if observed <= received_ms:  # never store a point from the future
                self._accept(contract.series, Tick(observed, received_ms, value, live=False), stats)

    def apply_tick(self, series: str, tick: Tick) -> bool:
        """Apply an already-decoded tick (PolyBolt adapter, tests)."""
        return self._accept(series, tick, self.series_stats[series])

    def apply_polybolt(self, frame: PolyBoltFrame, received_ms: int) -> int:
        """Apply a parsed PolyBolt frame (``data/polybolt.py``). Returns live ticks accepted."""
        wanted = self.secondary_symbol if frame.series == SECONDARY else self.symbol
        if wanted is None or frame.symbol != frame.expected_symbol:
            self.series_stats[frame.series].reject("wrong_symbol")
            return 0
        accepted = 0
        for observed, value in frame.points:
            if observed > received_ms:
                continue
            tick = Tick(observed, received_ms, value, live=not frame.snapshot)
            if self._accept(frame.series, tick, self.series_stats[frame.series]):
                accepted += int(tick.live)
        return accepted

    def _accept(self, series: str, tick: Tick, stats: SeriesStats) -> bool:
        if not tick.value.is_finite() or tick.value <= 0:
            stats.reject("malformed")
            return False
        target = self._series(series)
        if series in (SPOT, TWAP60) and not self._plausible(
            target.latest(), tick, tick.received_ms
        ):
            stats.reject("outlier")
            return False
        if not target.add(tick):
            stats.reject("out_of_order")
            return False
        if series == SPOT:
            self._update_vol(tick)
        if tick.live:
            stats.updates += 1
            self.last_live_ms[series] = tick.received_ms
        else:
            stats.history_points += 1
        return True

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
        """Chainlink spot vs secondary source; the only pair used by the stale check.

        The 60 s TWAP is excluded on purpose: it legitimately lags spot during moves.
        """
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
            detail = (
                "stream TWAP at window start (evidence-gated policy)"
                if accept_stream_only
                else "stream TWAP at window start only"
            )
            return PriceToBeat(stream.value, "rtds", accept_stream_only, detail)
        return PriceToBeat(None, "none", False, "price to beat unknown")

    # ------------------------------------------------------------------ diagnostics
    def diagnostics(self, now_ms: int) -> dict[str, Any]:
        """Raw, normalised and pairwise values behind the dispersion check."""
        latest = {s: self._series(s).latest() for s in SERIES}

        def value(s: str) -> float | None:
            t = latest[s]
            return None if t is None else float(t.value)

        def pair(a: str, b: str) -> float | None:
            va, vb = value(a), value(b)
            if va is None or vb is None or va <= 0 or vb <= 0:
                return None
            return round(abs(math.log(va / vb)) * 1e4, 3)

        contracts = {c.series: c for c in LEGACY_CONTRACTS.values()}
        reference = {}
        for s in SERIES:
            t = latest[s]
            c = contracts[s]
            reference[s] = {
                "value": value(s),
                "observed_ms": t.observed_ms if t else None,
                "age_ms": (now_ms - t.received_ms) if t else None,
                "live": t.live if t else None,
                "source": c.source,
                "quote": c.quote,
                "topic": c.topic,
                "full_accuracy_value_encoding": c.exact_encoding,
                "raw": self.last_raw.get(s),
            }
        final = self.dispersion_bps()
        return {
            "reference_values": reference,
            "normalized_values": {
                "spot_usd": value(SPOT),
                "twap60_usd": value(TWAP60),
                "secondary_usd": value(SECONDARY),
                "note": "secondary is quoted in USDT; no USDT->USD conversion is applied, "
                "so the USDT basis is part of the dispersion",
            },
            "dispersion_pairs_bps": {
                "spot/twap60": pair(SPOT, TWAP60),
                "spot/secondary": pair(SPOT, SECONDARY),
                "twap60/secondary": pair(TWAP60, SECONDARY),
            },
            "dispersion_final_bps": None if final is None else round(final, 3),
            "dispersion_formula": "|ln(spot / secondary)| * 1e4 (spot/secondary pair only)",
            "rejected_example": {
                s: self.last_raw[f"{s}_rejected"]
                for s in SERIES
                if f"{s}_rejected" in self.last_raw
            },
            "stats": {
                "frames": self.frames,
                "empty_frames": self.empty_frames,
                "malformed_frames": self.malformed_frames,
                "server_errors": self.server_errors,
                "last_server_error": self.last_server_error,
                "outliers": self.outliers,
                "series": {
                    s: {
                        "updates": st.updates,
                        "history_points": st.history_points,
                        "rejected": dict(st.rejected),
                        "stored_ticks": len(self._series(s)),
                    }
                    for s, st in self.series_stats.items()
                },
            },
        }


def _raw_fields(payload: dict[str, Any], decoded_as: str) -> dict[str, Any]:
    """The server's own strings, kept so a unit question can be settled from evidence."""
    return {
        "full_accuracy_value": str(payload.get("full_accuracy_value"))[:40]
        if "full_accuracy_value" in payload
        else None,
        "value": str(payload.get("value"))[:40],
        "decoded_as": decoded_as,
    }
