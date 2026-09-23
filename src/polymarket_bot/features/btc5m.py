"""Point-in-time features for BTC 5m markets.

Every feature is computed from state whose receive time is <= the snapshot
time (the hub never holds future data), so features are lookahead-free by
construction. ``FEATURE_VERSION`` must change whenever a definition changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from polymarket_bot.data.reference_prices import ReferencePriceState
from polymarket_bot.domain.snapshot import MarketSnapshot

FEATURE_VERSION = "btc5m-features-1.0.0"


@dataclass(frozen=True, slots=True)
class FeatureVector:
    feature_version: str
    t_ms: int
    tau_s: float  # time to window end (local clock)
    tau_eff_s: float  # window end minus the latest reference observation (>= tau_s)
    elapsed_s: float  # time since window start
    spot: float | None
    price_to_beat: float | None
    log_distance_bps: float | None  # ln(spot / price_to_beat) * 1e4
    sigma_per_sqrt_s: float | None  # log-price volatility per sqrt(second)
    vol_samples: int
    observed_avg: float | None  # spot average over [end - L, t] when t > end - L
    observed_coverage: float
    momentum_30s_bps: float | None
    momentum_120s_bps: float | None
    up_mid: float | None
    down_mid: float | None
    market_implied_up: float | None  # book-implied probability of Up
    up_spread: float | None
    imbalance_up: float | None
    dispersion_bps: float | None

    @property
    def complete(self) -> bool:
        return None not in (self.spot, self.price_to_beat, self.sigma_per_sqrt_s)


def _f(x: Decimal | None) -> float | None:
    return None if x is None else float(x)


def _momentum_bps(ref: ReferencePriceState, now_obs_ms: int, lookback_ms: int) -> float | None:
    latest = ref.spot.latest()
    past = ref.spot.at_or_before(now_obs_ms - lookback_ms)
    if latest is None or past is None or past.value <= 0:
        return None
    return math.log(float(latest.value) / float(past.value)) * 1e4


def compute_features(
    snapshot: MarketSnapshot, reference: ReferencePriceState, *, twap_lookback_s: int
) -> FeatureVector:
    now = snapshot.utc_ms
    market = snapshot.market
    ref = snapshot.reference
    spot = _f(ref.spot)
    ptb = _f(ref.price_to_beat) if ref.price_to_beat_verified else None
    log_dist = math.log(spot / ptb) * 1e4 if spot and ptb else None
    sigma, n = reference.vol_per_sqrt_s()
    lookback_ms = twap_lookback_s * 1000
    avg_start = market.window_end_ms - lookback_ms
    observed_avg: float | None = None
    coverage = 0.0
    latest_obs = ref.spot_observed_ms or now
    if latest_obs > avg_start:
        avg, coverage = reference.trailing_average(avg_start, min(latest_obs, market.window_end_ms))
        observed_avg = _f(avg)
    up, down = snapshot.quote("Up"), snapshot.quote("Down")
    up_mid, down_mid = _f(up.mid), _f(down.mid)
    implied: float | None = None
    if up_mid is not None and down_mid is not None:
        implied = (up_mid + (1.0 - down_mid)) / 2.0
    elif up_mid is not None:
        implied = up_mid
    return FeatureVector(
        feature_version=FEATURE_VERSION,
        t_ms=now,
        tau_s=snapshot.time_to_expiry_ms / 1000,
        tau_eff_s=max(snapshot.time_to_expiry_ms, market.window_end_ms - latest_obs) / 1000,
        elapsed_s=snapshot.time_since_start_ms / 1000,
        spot=spot,
        price_to_beat=ptb,
        log_distance_bps=log_dist,
        sigma_per_sqrt_s=sigma,
        vol_samples=n,
        observed_avg=observed_avg,
        observed_coverage=coverage,
        momentum_30s_bps=_momentum_bps(reference, latest_obs, 30_000),
        momentum_120s_bps=_momentum_bps(reference, latest_obs, 120_000),
        up_mid=up_mid,
        down_mid=down_mid,
        market_implied_up=implied,
        up_spread=_f(up.spread),
        imbalance_up=_f(up.imbalance),
        dispersion_bps=ref.dispersion_bps,
    )
