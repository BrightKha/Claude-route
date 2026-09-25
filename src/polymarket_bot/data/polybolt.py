"""PolyBolt reference-price channels — frame parser and subscription builder.

PolyBolt (``wss://ws-live-v2.polymarket.com/ws``) replaces the legacy RTDS price
topics (docs/research.md §4). VERIFIED against the official docs
(``/market-data/realtime-data`` and ``/migrate/rtds-to-polybolt``, read
2026-09-25) and ``polymarket-client`` 0.11.0
(``_internal/streams/realtime/protocol.py``):

* frame ``{"v":1,"channel","seq","ts","snapshot"?,"dropped"?,"payload"}``;
* channels ``price.crypto.twap`` (Chainlink 60 s TWAP, filter
  ``{"symbol":"btcusd","window_seconds":60}``) and ``price.crypto`` (**Pyth**,
  USD — not Chainlink, not Binance);
* canonical lowercase USD symbols (``btcusd``), ``filter`` is a JSON object;
* ``full_accuracy_value`` is an exact **decimal string** (no E18 scaling);
* each subscription first sends a history snapshot (``snapshot: true``,
  ``payload.data``), then live updates; ``seq`` is dense per channel on one
  connection and resets after a reconnect; ``dropped`` counts lost frames.

NOT WIRED to the runner, deliberately: PolyBolt price channels require CLOB API
credentials (``{"op":"auth"}``), and by the security model (CLAUDE.md invariant
4) only the live venue adapter may hold wallet-derived secrets. This module
therefore builds no auth frame and never sees a credential.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from polymarket_bot.data.reference_prices import SECONDARY, TWAP60, UnitError, parse_point

POLYBOLT_URL = "wss://ws-live-v2.polymarket.com/ws"
CHANNEL_TWAP = "price.crypto.twap"
CHANNEL_CRYPTO = "price.crypto"
TWAP_WINDOW_S = 60


@dataclass(frozen=True, slots=True)
class PolyBoltFrame:
    channel: str
    series: str
    symbol: str
    expected_symbol: str
    seq: int
    ts_ms: int
    snapshot: bool
    dropped: int | None
    points: tuple[tuple[int, Decimal], ...]  # (observed_ms, exact value)


@dataclass
class PolyBoltSequencer:
    """Per-connection, per-channel sequence tracking (gaps are data loss)."""

    last_seq: dict[str, int] = field(default_factory=dict)
    gaps: int = 0
    regressions: int = 0
    dropped: int = 0

    def observe(self, frame: PolyBoltFrame) -> list[str]:
        issues: list[str] = []
        last = self.last_seq.get(frame.channel)
        if last is not None:
            if frame.seq > last + 1:
                self.gaps += frame.seq - last - 1
                issues.append(f"gap {last}->{frame.seq}")
            elif frame.seq <= last:
                self.regressions += 1
                issues.append(f"seq regression {last}->{frame.seq}")
        if frame.dropped:
            self.dropped += frame.dropped
            issues.append(f"dropped {frame.dropped}")
        self.last_seq[frame.channel] = max(frame.seq, last or 0)
        return issues

    def reset(self) -> None:
        """A reconnect restarts every channel's sequence."""
        self.last_seq.clear()


def build_subscribe_frame(symbol: str, *, rid: str, with_spot: bool = False) -> str:
    """Subscribe frame for the 60 s TWAP (and optionally Pyth spot) of one USD symbol."""
    subs: list[dict[str, Any]] = [
        {"channel": CHANNEL_TWAP, "filter": {"symbol": symbol, "window_seconds": TWAP_WINDOW_S}}
    ]
    if with_spot:
        subs.append({"channel": CHANNEL_CRYPTO, "filter": {"symbol": symbol}})
    return json.dumps({"op": "subscribe", "rid": rid, "subscriptions": subs}, separators=(",", ":"))


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def parse_frame(
    text: str, *, symbol: str, crypto_series: str = SECONDARY
) -> tuple[PolyBoltFrame | None, str]:
    """Parse one text frame. Returns (frame, "") or (None, reason).

    ``crypto_series`` is the role given to ``price.crypto`` (Pyth). It defaults
    to the dispersion check; using it as the fair-value spot is a model change
    that needs evidence first (docs/research.md §4).
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None, "not json"
    if not isinstance(data, dict):
        return None, "not an object"
    if "op" in data and "channel" not in data:
        return None, f"control:{data.get('op')}"
    try:
        if data.get("v") != 1:
            return None, f"unsupported version {data.get('v')!r}"
        channel = str(data.get("channel"))
        if channel == CHANNEL_TWAP:
            series = TWAP60
        elif channel == CHANNEL_CRYPTO:
            series = crypto_series
        else:
            return None, f"unknown channel {channel!r}"
        seq = _nonnegative_int(data["seq"], "seq")
        ts_ms = _nonnegative_int(data["ts"], "ts")
        dropped = _nonnegative_int(data["dropped"], "dropped") if "dropped" in data else None
        snapshot = data.get("snapshot", False)
        if not isinstance(snapshot, bool):
            return None, "snapshot must be a boolean"
        payload = data["payload"]
        if not isinstance(payload, dict) or not isinstance(payload.get("symbol"), str):
            return None, "payload.symbol missing"
        if series == TWAP60 and payload.get("window_seconds") != TWAP_WINDOW_S:
            return None, "window_seconds must be 60"
        raw_points = payload["data"] if snapshot else [payload]
        if not isinstance(raw_points, list):
            return None, "snapshot data must be a list"
        points = []
        for point in raw_points:
            if not isinstance(point, dict):
                return None, "point must be an object"
            observed, value, _ = parse_point(point, "decimal")  # never E18 on PolyBolt
            points.append((observed, value))
    except UnitError as exc:
        return None, f"unit mismatch: {exc}"
    except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
        return None, f"malformed: {type(exc).__name__}"
    return (
        PolyBoltFrame(
            channel=channel,
            series=series,
            symbol=str(payload["symbol"]).lower(),
            expected_symbol=symbol,
            seq=seq,
            ts_ms=ts_ms,
            snapshot=snapshot,
            dropped=dropped,
            points=tuple(points),
        ),
        "",
    )
