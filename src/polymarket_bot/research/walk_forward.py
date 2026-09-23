"""Walk-forward calibration study (research only; never imported by production).

1. Build a dataset from a recorded session: every ``sample_every_s`` seconds,
   for each running market with a fresh snapshot, compute the production
   features and the baseline TWAP model probability *using only data received so
   far* (same hub, same simulated clock as replay). Outcomes are attached from
   the official resolutions at the end.
2. Chronological expanding-window folds: train a logistic calibrator on all
   windows before fold ``k`` and evaluate on fold ``k`` against the baseline and
   the market-implied probability (book mid).
3. Fit a final calibrator on everything and write it as a versioned JSON
   artifact loadable by ``LogisticCalibrator.load`` — enabling it in a config is
   a manual, reviewed decision (promotion is never automatic).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.data.replay import iter_messages, open_session
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.features.btc5m import compute_features
from polymarket_bot.market.hub import MarketDataHub
from polymarket_bot.research.metrics import brier, brier_skill, log_loss
from polymarket_bot.strategies.btc_5m.fair_value import TwapGaussianModel, baseline_logit
from polymarket_bot.watchdog.health import HealthRegistry

FEATURES = ("baseline_logit", "tau_s", "momentum_30s_bps", "imbalance_up")


@dataclass(frozen=True)
class Sample:
    window_start_ms: int
    t_ms: int
    baseline_p: float
    market_p: float | None
    tau_s: float
    momentum_30s_bps: float
    imbalance_up: float
    outcome_up: int | None = None

    def x(self) -> list[float]:
        return [
            baseline_logit(self.baseline_p),
            self.tau_s / 300.0,
            self.momentum_30s_bps / 10.0,
            self.imbalance_up,
        ]


def build_dataset(config: AppConfig, session_path: Path, sample_every_s: int = 15) -> list[Sample]:
    session = open_session(session_path)
    messages = iter_messages(session)
    first = next(messages, None)
    if first is None:
        return []
    clock = SimulatedClock(first.received_ms)
    hub = MarketDataHub(config, clock, HealthRegistry())
    model = TwapGaussianModel(config.fair_value)
    step = sample_every_s * 1000
    next_sample = first.received_ms - first.received_ms % step + step
    samples: list[Sample] = []

    def sample(t: int) -> None:
        clock.advance_to(t)
        for tracked in hub.active_markets(t):
            snap = hub.snapshot(tracked.definition.condition_id)
            if snap is None or not snap.is_fresh:
                continue
            fv = compute_features(
                snap, hub.reference, twap_lookback_s=config.fair_value.twap_lookback_s
            )
            est = model.estimate(fv)
            if not est.ok:
                continue
            samples.append(
                Sample(
                    window_start_ms=tracked.definition.window_start_ms,
                    t_ms=t,
                    baseline_p=est.p_up,
                    market_p=fv.market_implied_up,
                    tau_s=fv.tau_s,
                    momentum_30s_bps=fv.momentum_30s_bps or 0.0,
                    imbalance_up=fv.imbalance_up or 0.0,
                )
            )

    clock.advance_to(first.received_ms)
    hub.on_raw(first)
    for msg in messages:
        while next_sample < msg.received_ms:
            sample(next_sample)
            next_sample += step
        clock.advance_to(msg.received_ms)
        hub.on_raw(msg)
    winners = {tm.definition.window_start_ms: tm.winner for tm in hub.markets.values() if tm.winner}
    return [
        Sample(**{**asdict(s), "outcome_up": int(winners[s.window_start_ms] == "Up")})
        for s in samples
        if s.window_start_ms in winners
    ]


def fit_logistic(x: np.ndarray, y: np.ndarray, *, l2: float = 1.0, iters: int = 50) -> np.ndarray:
    """Newton-Raphson logistic regression with an L2 penalty (intercept unpenalised)."""
    xb = np.hstack([np.ones((x.shape[0], 1)), x])
    w = np.zeros(xb.shape[1])
    reg = np.eye(xb.shape[1]) * l2
    reg[0, 0] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(xb @ w)))
        grad = xb.T @ (p - y) + reg @ w
        hess = (xb.T * (p * (1 - p))) @ xb + reg
        step = np.linalg.solve(hess, grad)
        w -= step
        if float(np.max(np.abs(step))) < 1e-8:
            break
    return w


def _predict(w: np.ndarray, x: np.ndarray) -> np.ndarray:
    z = w[0] + x @ w[1:]
    return np.asarray(1.0 / (1.0 + np.exp(-z)))


def walk_forward(samples: list[Sample], folds: int = 4) -> dict[str, Any]:
    windows = sorted({s.window_start_ms for s in samples})
    if len(windows) < folds + 1:
        return {"error": f"need > {folds} resolved windows, have {len(windows)}"}
    chunks = [
        windows[i * len(windows) // (folds + 1) : (i + 1) * len(windows) // (folds + 1)]
        for i in range(folds + 1)
    ]
    results: list[dict[str, Any]] = []
    for k in range(1, folds + 1):
        train_w = {w for chunk in chunks[:k] for w in chunk}
        test_w = set(chunks[k])
        train = [s for s in samples if s.window_start_ms in train_w]
        test = [s for s in samples if s.window_start_ms in test_w]
        if not train or not test:
            continue
        w = fit_logistic(
            np.array([s.x() for s in train]),
            np.array([s.outcome_up for s in train], dtype=float),
        )
        cal = _predict(w, np.array([s.x() for s in test]))
        y = [int(s.outcome_up or 0) for s in test]
        base = list(zip([s.baseline_p for s in test], y, strict=True))
        calibrated = list(zip([float(p) for p in cal], y, strict=True))
        market = [(s.market_p, yy) for s, yy in zip(test, y, strict=True) if s.market_p is not None]
        results.append(
            {
                "fold": k,
                "train_windows": len(train_w),
                "test_windows": len(test_w),
                "test_samples": len(test),
                "brier_baseline": brier(base),
                "brier_calibrated": brier(calibrated),
                "brier_market_mid": brier(market),
                "log_loss_baseline": log_loss(base),
                "log_loss_calibrated": log_loss(calibrated),
                "calibration_improves": (brier(calibrated) or math.inf) < (brier(base) or 0),
                "baseline_skill_vs_market": brier_skill(brier(base), brier(market)),
            }
        )
    return {"folds": results}


def fit_final_calibrator(samples: list[Sample], *, synthetic: bool, version: str) -> dict[str, Any]:
    x = np.array([s.x() for s in samples])
    y = np.array([s.outcome_up for s in samples], dtype=float)
    w = fit_logistic(x, y)
    rows = json.dumps([asdict(s) for s in samples], sort_keys=True).encode()
    # The production calibrator applies coefficients to raw inputs; fold the scaling in.
    scale = np.array([1.0, 1 / 300.0, 1 / 10.0, 1.0])
    return {
        "version": version,
        "features": list(FEATURES),
        "coef": [float(c) for c in w[1:] * scale],
        "intercept": float(w[0]),
        "dataset_sha256": hashlib.sha256(rows).hexdigest(),
        "metrics": {"n_samples": len(samples), "synthetic": synthetic},
        "warning": "SYNTHETIC — do not enable" if synthetic else "review before enabling",
    }


def run_study(config: AppConfig, session_path: Path, out_dir: Path) -> dict[str, Any]:
    session = open_session(session_path)
    samples = build_dataset(config, session_path)
    study = {
        "session": str(session_path),
        "synthetic": session.synthetic,
        "samples": len(samples),
        "windows": len({s.window_start_ms for s in samples}),
        **walk_forward(samples),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "walk_forward.json").write_text(json.dumps(study, indent=2), encoding="utf-8")
    if samples:
        artifact = fit_final_calibrator(
            samples, synthetic=session.synthetic, version=f"logit-cal-{len(samples)}"
        )
        (out_dir / "calibrator.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return study
