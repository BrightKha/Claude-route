"""SYNTHETIC BTC 5m sessions in the exact recorded format (plumbing tests only).

WARNING — read before using any number produced from these sessions:

* the BTC path, the order books, the market maker and the resolutions are all
  simulated; ``session.json`` is flagged ``synthetic: true`` and the promotion
  gates refuse synthetic evidence;
* the simulated market maker is *assumed* to misprice by a noisy logit error
  (``mm_noise``) and to see reference data ``mm_lag_s`` late. Any edge the bot
  finds here exists because of these assumptions — PnL on synthetic data says
  NOTHING about profitability on Polymarket;
* outcomes follow the same arithmetic-TWAP rule the fair-value model assumes,
  so the model is calibrated on this data by construction.

What it *is* good for: exercising discovery/validation, book maintenance
(snapshots + deltas + echoes), reference prices, price-to-beat verification,
the decision loop, paper execution, exits, settlement, reconciliation,
disconnect handling and the metrics code, end to end and deterministically.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from polymarket_bot.data.recorder import SessionRecorder
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.ports import RawMessage

TEMPLATE = Path(__file__).parent / "templates" / "btc5m_twap60_market_template.json"
WINDOW_MS = 300_000
TWAP_S = 60
TICK = 0.01
DEFAULT_START_MS = int(datetime(2026, 9, 15, tzinfo=UTC).timestamp() * 1000)


@dataclass(frozen=True)
class SynthParams:
    start_ms: int = DEFAULT_START_MS
    windows: int = 24
    seed: int = 7
    spot0: float = 87_000.0
    sigma_bps: float = 1.0  # log-price vol per sqrt(second), in bps
    regime_dispersion: float = 0.4  # sd of the per-window log vol multiplier
    jump_prob_per_s: float = 0.0003
    jump_bps: float = 20.0
    mm_noise: float = 0.35  # sd of the market maker's logit error (ASSUMPTION)
    mm_noise_halflife_s: float = 30.0
    mm_lag_s: int = 2
    spread_ticks: tuple[int, int] = (1, 3)
    level_size: tuple[int, int] = (20, 300)
    levels: int = 3
    rtds_lag_ms: tuple[int, int] = (300, 900)
    ws_lag_ms: tuple[int, int] = (60, 160)
    book_snapshot_every_s: int = 15
    discovery_every_s: int = 30
    discover_before_s: int = 120
    resolution_delay_s: int = 20
    warmup_s: int = 300
    disconnect_prob_per_window: float = 0.0


@dataclass
class _Window:
    index: int
    start_ms: int
    slug: str
    condition_id: str
    up_token: str
    down_token: str
    vol_mult: float
    ptb: float | None = None
    final: float | None = None
    noise: float = 0.0
    last_levels: dict[str, tuple[dict[str, int], dict[str, int]]] = field(default_factory=dict)

    @property
    def end_ms(self) -> int:
        return self.start_ms + WINDOW_MS


def _digest(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def _token(seed: int, start: int, outcome: str) -> str:
    return str(int(_digest("synthetic-token", seed, start, outcome), 16) % 10**77).rjust(77, "1")


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def mm_probability(
    spot: float, ptb: float | None, tau_s: float, observed_avg: float | None, sigma: float
) -> float:
    """P(final 60s TWAP >= price to beat) for a driftless random walk (sigma per sqrt s)."""
    if ptb is None:
        return 0.5
    lookback = float(TWAP_S)
    if tau_s >= lookback or observed_avg is None:
        mean = spot
        var = (spot * sigma) ** 2 * max(tau_s - 2 * lookback / 3, 0.0)
    else:
        mean = ((lookback - tau_s) / lookback) * observed_avg + (tau_s / lookback) * spot
        var = (spot * sigma) ** 2 * tau_s**3 / (3 * lookback**2)
    if var <= 0:
        return 1.0 if mean >= ptb else 0.0
    return _phi((mean - ptb) / math.sqrt(var))


class _Generator:
    def __init__(self, params: SynthParams) -> None:
        if params.start_ms % WINDOW_MS:
            raise ValueError("start_ms must be aligned to a 5-minute boundary")
        self.p = params
        self.rng = random.Random(params.seed)  # noqa: S311 - simulation only
        self.template = json.loads(TEMPLATE.read_text(encoding="utf-8"))[0]
        self.out: list[tuple[int, int, str, str, Any]] = []
        self._order = 0
        self.spot_hist: deque[tuple[int, float]] = deque(maxlen=TWAP_S)
        self.all_spots: dict[int, float] = {}
        self.windows = [self._make_window(i) for i in range(params.windows)]
        self.disconnected_until: int = -1

    def _make_window(self, i: int) -> _Window:
        start = self.p.start_ms + i * WINDOW_MS
        mult = math.exp(self.rng.gauss(0.0, self.p.regime_dispersion))
        return _Window(
            index=i,
            start_ms=start,
            slug=f"btc-updown-5m-{start // 1000}",
            condition_id="0x" + _digest("synthetic-condition", self.p.seed, start),
            up_token=_token(self.p.seed, start, "Up"),
            down_token=_token(self.p.seed, start, "Down"),
            vol_mult=mult,
        )

    def emit(self, t: int, src: str, kind: str, payload: Any) -> None:
        self._order += 1
        self.out.append((t, self._order, src, kind, payload))

    # ------------------------------------------------------------------ reference
    def _sigma_at(self, ms: int) -> float:
        for w in self.windows:
            if w.start_ms <= ms < w.end_ms:
                return self.p.sigma_bps * 1e-4 * w.vol_mult
        return self.p.sigma_bps * 1e-4

    def _step_spot(self, ms: int, spot: float) -> float:
        sigma = self._sigma_at(ms)
        r = self.rng.gauss(0.0, sigma)
        if self.rng.random() < self.p.jump_prob_per_s:
            r += self.rng.choice((-1, 1)) * self.p.jump_bps * 1e-4
        return spot * math.exp(r)

    def _twap(self) -> float:
        return sum(v for _, v in self.spot_hist) / len(self.spot_hist)

    def _emit_reference(self, ms: int, spot: float, twap: float) -> None:
        lo, hi = self.p.rtds_lag_ms
        for topic, symbol, value, extra in (
            ("crypto_prices_chainlink", "btc/usd", spot, {}),
            (
                "crypto_prices_twap_sixty",
                "btc/usd",
                twap,
                {"full_accuracy_value": str(round(twap * 10**18)), "window_s": TWAP_S},
            ),
            ("crypto_prices", "btcusdt", spot * (1 + self.rng.gauss(0.0, 1e-4)), {}),
        ):
            recv = ms + self.rng.randint(lo, hi)
            payload = {"symbol": symbol, "timestamp": ms, "value": value, **extra}
            frame = {"topic": topic, "type": "update", "timestamp": recv - 20, "payload": payload}
            self.emit(recv, "rtds", "ws_frame", json.dumps(frame, separators=(",", ":")))

    # ------------------------------------------------------------------ gamma
    def _gamma_market(self, w: _Window, ms: int) -> dict[str, Any]:
        m: dict[str, Any] = copy.deepcopy(self.template)
        event = m["events"][0]
        m.update(
            {
                "id": str(9_000_000 + w.index),
                "question": f"Bitcoin Up or Down - SYNTHETIC {_iso(w.start_ms)}",
                "conditionId": w.condition_id,
                "slug": w.slug,
                "eventStartTime": _iso(w.start_ms),
                "endDate": _iso(w.end_ms),
                "startDate": _iso(w.start_ms - 86_400_000),
                "clobTokenIds": json.dumps([w.up_token, w.down_token]),
                "synthetic": True,
            }
        )
        event.update(
            {
                "id": str(8_000_000 + w.index),
                "slug": w.slug,
                "startTime": _iso(w.start_ms),
                "endDate": _iso(w.end_ms),
            }
        )
        resolved = w.final is not None and ms >= w.end_ms + self.p.resolution_delay_s * 1000
        meta: dict[str, float] = {}
        if w.ptb is not None:
            meta["priceToBeat"] = w.ptb
        if resolved:
            assert w.final is not None
            assert w.ptb is not None
            meta["finalPrice"] = w.final
            up_won = w.final >= w.ptb
            m.update(
                {
                    "closed": True,
                    "acceptingOrders": False,
                    "umaResolutionStatus": "resolved",
                    "outcomePrices": json.dumps(["1", "0"] if up_won else ["0", "1"]),
                }
            )
            event["closed"] = True
        elif ms >= w.end_ms:
            m["acceptingOrders"] = False
        if meta:
            event["eventMetadata"] = meta
        return m

    def _emit_gamma(self, ms: int) -> None:
        live = []
        for w in self.windows:
            first = w.start_ms - self.p.discover_before_s * 1000
            last = w.end_ms + (self.p.resolution_delay_s + 2 * self.p.discovery_every_s) * 1000
            if first <= ms <= last:
                live.append(self._gamma_market(w, ms))
        if live:
            self.emit(ms + self.rng.randint(80, 250), "gamma", "markets", live)

    # ------------------------------------------------------------------ books
    def _levels(
        self, belief: float, sizes_rng: random.Random
    ) -> tuple[dict[str, int], dict[str, int]]:
        lo, hi = self.p.spread_ticks
        mid_ticks = round(belief / TICK)
        bid_t = max(1, min(98, mid_ticks - self.rng.randint(1, max(1, hi - lo + 1))))
        ask_t = min(99, max(bid_t + 1, mid_ticks + self.rng.randint(1, max(1, hi - lo + 1))))
        smin, smax = self.p.level_size
        bids = {
            f"{(bid_t - k) * TICK:.2f}": sizes_rng.randint(smin, smax)
            for k in range(self.p.levels)
            if bid_t - k >= 1
        }
        asks = {
            f"{(ask_t + k) * TICK:.2f}": sizes_rng.randint(smin, smax)
            for k in range(self.p.levels)
            if ask_t + k <= 99
        }
        return bids, asks

    @staticmethod
    def _complement(
        up: tuple[dict[str, int], dict[str, int]],
    ) -> tuple[dict[str, int], dict[str, int]]:
        bids, asks = up
        down_bids = {f"{1 - float(p):.2f}": s for p, s in asks.items()}
        down_asks = {f"{1 - float(p):.2f}": s for p, s in bids.items()}
        return down_bids, down_asks

    def _emit_book(self, w: _Window, ms: int, *, snapshot: bool) -> None:
        belief = self._belief(w, ms)
        up = self._levels(belief, self.rng)
        books = {w.up_token: up, w.down_token: self._complement(up)}
        lo, hi = self.p.ws_lag_ms
        recv = ms + self.rng.randint(lo, hi)
        events: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        for token, (bids, asks) in books.items():
            prev = w.last_levels.get(token)
            best_bid = max(bids, key=float) if bids else ""
            best_ask = min(asks, key=float) if asks else ""
            if snapshot or prev is None:
                events.append(
                    {
                        "event_type": "book",
                        "asset_id": token,
                        "market": w.condition_id,
                        "bids": [{"price": p, "size": str(s)} for p, s in bids.items()],
                        "asks": [{"price": p, "size": str(s)} for p, s in asks.items()],
                        "timestamp": str(ms),
                        "hash": _digest(token, ms)[:40],
                        "tick_size": "0.01",
                    }
                )
            else:
                for side, new, old in (("BUY", bids, prev[0]), ("SELL", asks, prev[1])):
                    for price in sorted(set(new) | set(old)):
                        size = new.get(price, 0)
                        if old.get(price) != size:
                            changes.append(
                                {
                                    "asset_id": token,
                                    "price": price,
                                    "size": str(size),
                                    "side": side,
                                    "best_bid": best_bid,
                                    "best_ask": best_ask,
                                }
                            )
            w.last_levels[token] = (bids, asks)
        if changes:
            events.append(
                {
                    "event_type": "price_change",
                    "market": w.condition_id,
                    "price_changes": changes,
                    "timestamp": str(ms),
                }
            )
        if events:
            self.emit(recv, "clob_ws", "ws_frame", json.dumps(events, separators=(",", ":")))

    def _belief(self, w: _Window, ms: int) -> float:
        lag_ms = ms - self.p.mm_lag_s * 1000
        spot = self.all_spots.get(lag_ms - lag_ms % 1000)
        decay = 0.5 ** (1.0 / self.p.mm_noise_halflife_s)
        w.noise = decay * w.noise + math.sqrt(1 - decay**2) * self.rng.gauss(0.0, self.p.mm_noise)
        if spot is None or ms < w.start_ms:
            p = 0.5
        else:
            tau = (w.end_ms - lag_ms) / 1000
            avg = None
            if tau < TWAP_S:
                pts = [
                    v for t, v in self.all_spots.items() if w.end_ms - TWAP_S * 1000 < t <= lag_ms
                ]
                avg = sum(pts) / len(pts) if pts else None
            p = mm_probability(spot, w.ptb, tau, avg, self._sigma_at(ms))
        p = min(max(p, 0.005), 0.995)
        logit = math.log(p / (1 - p)) + w.noise
        return 1 / (1 + math.exp(-logit))

    # ------------------------------------------------------------------ main loop
    def run(self) -> None:
        p = self.p
        begin = p.start_ms - p.warmup_s * 1000
        end = p.start_ms + p.windows * WINDOW_MS
        stop = end + (p.resolution_delay_s + 2 * p.discovery_every_s + 5) * 1000
        self.emit(begin, "clob_ws", "connection", {"state": "connected"})
        self.emit(begin, "rtds", "connection", {"state": "connected"})
        chaos = random.Random(p.seed + 1)  # noqa: S311 - simulation only
        disconnects = {
            w.start_ms + chaos.randint(30, 240) * 1000: chaos.randint(5, 20) * 1000
            for w in self.windows
            if chaos.random() < p.disconnect_prob_per_window
        }
        spot = p.spot0
        for ms in range(begin, stop, 1000):
            spot = self._step_spot(ms, spot)
            self.spot_hist.append((ms, spot))
            self.all_spots[ms] = spot
            twap = self._twap()
            for w in self.windows:
                if ms == w.start_ms:
                    w.ptb = twap
                if ms == w.end_ms:
                    w.final = twap
            self._emit_reference(ms, spot, twap)
            if (ms - begin) % (p.discovery_every_s * 1000) == 0:
                self._emit_gamma(ms)
            if ms in disconnects:
                self.emit(ms, "clob_ws", "connection", {"state": "disconnected", "reason": "chaos"})
                self.disconnected_until = ms + disconnects[ms]
                for w in self.windows:
                    w.last_levels.clear()
                continue
            if self.disconnected_until == ms:
                self.emit(ms, "clob_ws", "connection", {"state": "connected"})
            if ms < self.disconnected_until:
                continue
            for w in self.windows:
                first = w.start_ms - p.discover_before_s * 1000
                if first + 1000 <= ms < w.end_ms:
                    snapshot = (ms - first) % (p.book_snapshot_every_s * 1000) == 0
                    self._emit_book(w, ms, snapshot=snapshot)
            self._prune(ms)

    def _prune(self, ms: int) -> None:
        horizon = ms - (WINDOW_MS + 60_000)
        for t in [t for t in self.all_spots if t < horizon]:
            del self.all_spots[t]


def generate_session(out_dir: Path, params: SynthParams, session_id: str | None = None) -> Path:
    """Write a SYNTHETIC session directory and return its path."""
    gen = _Generator(params)
    gen.run()
    gen.out.sort(key=lambda x: (x[0], x[1]))
    sid = session_id or f"synthetic-{params.seed}-{params.start_ms // 1000}-{params.windows}w"
    recorder = SessionRecorder(out_dir, sid, SimulatedClock(gen.out[0][0]), synthetic=True)
    for t, _, src, kind, payload in gen.out:
        recorder.record_raw(RawMessage(src, kind, payload, t, t * 1_000_000))
    recorder.close()
    summary = {
        "synthetic": True,
        "params": {k: getattr(params, k) for k in params.__dataclass_fields__},
        "windows": [
            {
                "slug": w.slug,
                "price_to_beat": w.ptb,
                "final": w.final,
                "winner": None
                if w.final is None or w.ptb is None
                else ("Up" if w.final >= w.ptb else "Down"),
                "vol_mult": round(w.vol_mult, 4),
            }
            for w in gen.windows
        ],
        "messages": len(gen.out),
    }
    (recorder.directory / "synthetic_truth.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return recorder.directory
