"""Pure metric functions for backtests and calibration studies (research only)."""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Callable, Iterable, Sequence
from typing import Any

EPS = 1e-6


def brier(pairs: Sequence[tuple[float, int]]) -> float | None:
    """Mean squared error of probabilities against 0/1 outcomes."""
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def log_loss(pairs: Sequence[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    total = 0.0
    for p, y in pairs:
        q = min(max(p, EPS), 1 - EPS)
        total -= y * math.log(q) + (1 - y) * math.log(1 - q)
    return total / len(pairs)


def brier_skill(model: float | None, reference: float | None) -> float | None:
    """1 - model/reference; > 0 means the model beats the reference."""
    if model is None or reference is None or reference <= 0:
        return None
    return 1.0 - model / reference


def calibration_table(pairs: Sequence[tuple[float, int]], bins: int = 10) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(p, y) for p, y in pairs if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if sel:
            rows.append(
                {
                    "bin": f"[{lo:.1f},{hi:.1f})",
                    "n": len(sel),
                    "mean_predicted": sum(p for p, _ in sel) / len(sel),
                    "observed_rate": sum(y for _, y in sel) / len(sel),
                }
            )
    return rows


def max_drawdown(equity: Sequence[float]) -> tuple[float, float]:
    """(max drawdown in currency, as a fraction of the running peak)."""
    peak = -math.inf
    worst_abs = 0.0
    worst_pct = 0.0
    for value in equity:
        peak = max(peak, value)
        dd = peak - value
        worst_abs = max(worst_abs, dd)
        if peak > 0:
            worst_pct = max(worst_pct, dd / peak)
    return worst_abs, worst_pct


def bootstrap_sum_ci(
    values: Sequence[float], *, samples: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the *sum* (total PnL) over resampled trades."""
    if len(values) < 2:
        return None
    rng = random.Random(seed)  # noqa: S311 - statistics only
    n = len(values)
    sums = sorted(sum(rng.choice(values) for _ in range(n)) for _ in range(samples))
    lo = sums[int(alpha / 2 * samples)]
    hi = sums[min(samples - 1, int((1 - alpha / 2) * samples))]
    return lo, hi


def group_by[T](
    items: Iterable[T], key: Callable[[T], str], value: Callable[[T], float]
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for item in items:
        k = key(item)
        row = out.setdefault(k, {"n": 0.0, "pnl": 0.0})
        row["n"] += 1
        row["pnl"] += value(item)
    for row in out.values():
        row["mean"] = row["pnl"] / row["n"]
    return dict(sorted(out.items()))


def bucket(value: float | None, edges: Sequence[float], unit: str = "") -> str:
    if value is None:
        return "unknown"
    for lo, hi in itertools.pairwise(edges):
        if lo <= value < hi:
            return f"[{lo:g},{hi:g}){unit}"
    return f">={edges[-1]:g}{unit}" if value >= edges[-1] else f"<{edges[0]:g}{unit}"
