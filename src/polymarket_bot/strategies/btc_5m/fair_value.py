"""Fair probability of "Up" for BTC 5m TWAP-settled markets.

Baseline model (``TwapGaussianModel``), deliberately simple and interpretable:

Settlement ``F`` = Chainlink TWAP over the last ``L`` seconds of the window;
the price to beat ``K`` is the same feed at window start (docs/research.md §5).
Treat the price as driftless arithmetic Brownian motion with volatility
``S*sigma`` per sqrt(second), estimated from Chainlink spot. With ``tau``
seconds left:

* ``tau >= L``:  E[F] = S,                                Var[F] = (S sigma)^2 (tau - 2L/3)
* ``0 < tau < L``: E[F] = ((L-tau)/L) A + (tau/L) S,      Var[F] = (S sigma)^2 tau^3 / (3 L^2)
  where ``A`` is the observed average over [end-L, now].

Plus an explicit model-error variance (Chainlink's TWAP sampling/weighting is
not published, and Chainlink spot vs. our feed may differ):
``Var += (model_error_bps * 1e-4 * K)^2``.

``P(Up) = Phi((E[F] - K) / sd)`` (ties have probability ~0 under a continuous model).

Uncertainty band: min/max of P over sigma*(1 +/- u) and mean shifted by
+/- the model error. The band — not the point estimate — feeds the
conservative edge. Optional logistic calibration can only *widen* the band.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polymarket_bot.config.app_config import FairValueConfig
from polymarket_bot.features.btc5m import FeatureVector

P_FLOOR = 0.001  # never claim certainty


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True, slots=True)
class FairValueEstimate:
    ok: bool
    p_up: float
    p_lower: float
    p_upper: float
    model_version: str
    reasons: tuple[str, ...] = ()
    components: dict[str, float] = field(default_factory=dict)
    conflict: bool = False

    def for_outcome(self, outcome: str, up_label: str = "Up") -> tuple[float, float, float]:
        """(fair, lower, upper) for the given outcome token."""
        if outcome == up_label:
            return self.p_up, self.p_lower, self.p_upper
        return 1.0 - self.p_up, 1.0 - self.p_upper, 1.0 - self.p_lower


def _clip(p: float) -> float:
    return min(1.0 - P_FLOOR, max(P_FLOOR, p))


def twap_moments(
    *, spot: float, tau_s: float, lookback_s: float, sigma: float, observed_avg: float | None
) -> tuple[float, float]:
    """Mean and variance (price units) of the settlement TWAP given info at t."""
    if tau_s >= lookback_s:
        return spot, (spot * sigma) ** 2 * (tau_s - 2.0 * lookback_s / 3.0)
    if observed_avg is None:
        raise ValueError("observed average required inside the averaging window")
    w_obs = (lookback_s - tau_s) / lookback_s
    mean = w_obs * observed_avg + (1.0 - w_obs) * spot
    var = (spot * sigma) ** 2 * tau_s**3 / (3.0 * lookback_s**2)
    return mean, var


class TwapGaussianModel:
    def __init__(self, config: FairValueConfig) -> None:
        self._cfg = config
        self.version = config.model_version

    def estimate(self, fv: FeatureVector) -> FairValueEstimate:
        cfg = self._cfg
        reasons: list[str] = []
        if fv.spot is None:
            reasons.append("spot missing")
        if fv.price_to_beat is None:
            reasons.append("price to beat unknown/unverified")
        if fv.sigma_per_sqrt_s is None or fv.vol_samples < cfg.vol_min_samples:
            reasons.append(f"volatility not ready ({fv.vol_samples} samples)")
        if fv.tau_s <= 0:
            reasons.append("window ended")
        lookback = float(cfg.twap_lookback_s)
        tau = fv.tau_eff_s  # unknown part starts at the latest observation, not "now"
        if 0 < tau < lookback and (fv.observed_avg is None or fv.observed_coverage < 0.9):
            reasons.append(f"averaging window coverage {fv.observed_coverage:.2f} < 0.90")
        if reasons:
            return FairValueEstimate(False, 0.5, 0.0, 1.0, self.version, tuple(reasons))
        assert fv.spot is not None
        assert fv.price_to_beat is not None
        assert fv.sigma_per_sqrt_s is not None
        floor = cfg.vol_floor_bps_per_sqrt_s * 1e-4
        cap = cfg.vol_cap_bps_per_sqrt_s * 1e-4
        sigma = min(cap, max(floor, fv.sigma_per_sqrt_s))
        k = fv.price_to_beat
        err_sd = cfg.model_error_bps * 1e-4 * k

        def prob(sig: float, shift: float) -> float:
            mean, var = twap_moments(
                spot=fv.spot or 0.0,
                tau_s=tau,
                lookback_s=lookback,
                sigma=sig,
                observed_avg=fv.observed_avg,
            )
            sd = math.sqrt(var + err_sd**2)
            return norm_cdf((mean + shift - k) / sd)

        p = prob(sigma, 0.0)
        u = cfg.vol_uncertainty_mult
        grid = [prob(sigma * f, s) for f in (1.0 - u, 1.0 + u) for s in (-err_sd, err_sd)]
        lower, upper = min([p, *grid]), max([p, *grid])
        return FairValueEstimate(
            ok=True,
            p_up=_clip(p),
            p_lower=_clip(lower),
            p_upper=_clip(upper),
            model_version=self.version,
            components={
                "sigma_used": sigma,
                "err_sd_price": err_sd,
                "distance_bps": fv.log_distance_bps or 0.0,
                "tau_eff_s": tau,
            },
        )


@dataclass(frozen=True, slots=True)
class LogisticCalibrator:
    """Offline-trained logistic calibration on top of the baseline (research output).

    Loaded from a JSON artifact produced by ``research/calibration.py`` with
    version, dataset hash, feature list, coefficients and OOS metrics. It is
    only used if explicitly configured, and it can only widen the band.
    """

    version: str
    features: tuple[str, ...]
    coef: tuple[float, ...]
    intercept: float
    dataset_sha256: str
    metrics: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> LogisticCalibrator:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            version=str(data["version"]),
            features=tuple(data["features"]),
            coef=tuple(float(c) for c in data["coef"]),
            intercept=float(data["intercept"]),
            dataset_sha256=str(data["dataset_sha256"]),
            metrics=dict(data.get("metrics", {})),
        )

    def predict(self, inputs: dict[str, float]) -> float | None:
        try:
            pairs = zip(self.coef, self.features, strict=True)
            z = self.intercept + sum(c * inputs[f] for c, f in pairs)
        except KeyError:
            return None
        return 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, z))))


def baseline_logit(p: float) -> float:
    p = _clip(p)
    return math.log(p / (1 - p))


class FairValueEngine:
    """Baseline model + optional calibrator; flags conflicts between them."""

    def __init__(
        self,
        config: FairValueConfig,
        calibrator: LogisticCalibrator | None = None,
        *,
        conflict_threshold: float = 0.10,
    ) -> None:
        self.baseline = TwapGaussianModel(config)
        self.calibrator = calibrator
        self._conflict = conflict_threshold

    @property
    def version(self) -> str:
        cal = f"+{self.calibrator.version}" if self.calibrator else ""
        return f"{self.baseline.version}{cal}"

    def estimate(self, fv: FeatureVector) -> FairValueEstimate:
        base = self.baseline.estimate(fv)
        if not base.ok or self.calibrator is None:
            return base
        inputs = {
            "baseline_logit": baseline_logit(base.p_up),
            "tau_s": fv.tau_s,
            "momentum_30s_bps": fv.momentum_30s_bps or 0.0,
            "imbalance_up": fv.imbalance_up or 0.0,
        }
        cal = self.calibrator.predict(inputs)
        if cal is None:
            return base
        conflict = abs(cal - base.p_up) > self._conflict
        return FairValueEstimate(
            ok=True,
            p_up=_clip(cal),
            p_lower=_clip(min(base.p_lower, cal)),
            p_upper=_clip(max(base.p_upper, cal)),
            model_version=self.version,
            reasons=("model conflict",) if conflict else (),
            components={**base.components, "baseline_p": base.p_up, "calibrated_p": cal},
            conflict=conflict,
        )
