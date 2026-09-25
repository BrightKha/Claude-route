"""Evidence for (and gate of) the in-window price-to-beat policy.

Gamma publishes ``eventMetadata.priceToBeat`` only after a window ends
(docs/research.md §5), so during a window the only candidate is the RTDS 60 s
TWAP tick observed exactly at the window start. Each time Gamma's official
value first appears, the hub records one observation::

    window, official_price_to_beat, rtds_twap_at_window_start, difference_bps

This module turns those observations into ``PRICE_TO_BEAT_VALIDATION`` stats
and decides whether the (OFF by default) stream policy may be used:

* policy ``off`` (default)            -> never; NO_TRADE without the official value;
* policy ``evidence_gated``           -> only while N_WINDOWS >= min_windows
  (>= 100, enforced by config) **and** every paired window matched within
  ``stream_price_to_beat_max_diff_bps`` (MATCH_COUNT == N_WINDOWS). A single
  mismatch closes the gate. Synthetic observations never count.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from polymarket_bot.config.app_config import FairValueConfig

MISMATCHES_KEPT = 10


@dataclass(frozen=True, slots=True)
class PtbValidationStats:
    n_official: int  # windows whose official value was observed
    n_windows: int  # N_WINDOWS: official AND stream tick at the window start
    missing_stream: int  # official seen but no stream tick exactly at the start
    match_count: int  # MATCH_COUNT: |diff| <= max_diff_bps
    max_abs_diff_bps: float | None
    p95_abs_diff_bps: float | None
    mean_abs_diff_bps: float | None
    first_window_ms: int | None
    last_window_ms: int | None
    mismatches: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "N_WINDOWS": self.n_windows,
            "MATCH_COUNT": self.match_count,
            "MAX_ABS_DIFF_BPS": self.max_abs_diff_bps,
            "P95_ABS_DIFF_BPS": self.p95_abs_diff_bps,
            "MEAN_ABS_DIFF_BPS": self.mean_abs_diff_bps,
            "N_OFFICIAL": self.n_official,
            "MISSING_STREAM": self.missing_stream,
            "FIRST_WINDOW_MS": self.first_window_ms,
            "LAST_WINDOW_MS": self.last_window_ms,
            "MISMATCHES": list(self.mismatches),
        }


def _p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def compute_stats(observations: list[dict[str, Any]], *, max_diff_bps: float) -> PtbValidationStats:
    real = [o for o in observations if not o.get("synthetic")]
    paired = [o for o in real if o.get("diff_bps") is not None]
    diffs = [abs(float(o["diff_bps"])) for o in paired]
    windows = [int(o["window_start_ms"]) for o in real]
    return PtbValidationStats(
        n_official=len(real),
        n_windows=len(paired),
        missing_stream=len(real) - len(paired),
        match_count=sum(d <= max_diff_bps for d in diffs),
        max_abs_diff_bps=max(diffs) if diffs else None,
        p95_abs_diff_bps=_p95(diffs) if diffs else None,
        mean_abs_diff_bps=sum(diffs) / len(diffs) if diffs else None,
        first_window_ms=min(windows) if windows else None,
        last_window_ms=max(windows) if windows else None,
        mismatches=tuple(
            str(o["slug"]) for o in paired if abs(float(o["diff_bps"])) > max_diff_bps
        )[-MISMATCHES_KEPT:],
    )


def gate(stats: PtbValidationStats, cfg: FairValueConfig) -> tuple[bool, list[str]]:
    """(stream price to beat allowed, reasons it is not)."""
    reasons: list[str] = []
    if cfg.stream_price_to_beat_policy != "evidence_gated":
        reasons.append("policy off")
    if stats.n_windows < cfg.stream_price_to_beat_min_windows:
        reasons.append(f"{stats.n_windows} < {cfg.stream_price_to_beat_min_windows} windows")
    if stats.match_count < stats.n_windows:
        reasons.append(f"{stats.n_windows - stats.match_count} mismatching windows")
    return not reasons, reasons


@dataclass
class PriceToBeatValidator:
    """Live accumulation of observations; persistence through ``sink``."""

    cfg: FairValueConfig
    source: str
    synthetic: bool
    sink: Callable[[dict[str, Any]], object] | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)
    new_mismatches: list[str] = field(default_factory=list)
    _stats: PtbValidationStats | None = None

    def seed(self, stored: list[dict[str, Any]]) -> None:
        """Evidence accumulated by earlier sessions (state DB)."""
        known = {o["slug"] for o in self.observations}
        self.observations.extend(o for o in stored if o["slug"] not in known)
        self._stats = None

    def observe(
        self,
        *,
        slug: str,
        window_start_ms: int,
        official: Decimal,
        stream: Decimal | None,
        diff_bps: float | None,
        first_seen_ms: int,
    ) -> dict[str, Any]:
        obs: dict[str, Any] = {
            "slug": slug,
            "window_start_ms": window_start_ms,
            "official": official,
            "stream": stream,
            "diff_bps": diff_bps,
            "first_seen_ms": first_seen_ms,
            "source": self.source,
            "synthetic": self.synthetic,
        }
        if any(o["slug"] == slug for o in self.observations):
            return obs
        self.observations.append(obs)
        self._stats = None
        if self.sink is not None:
            self.sink(obs)
        if diff_bps is not None and abs(diff_bps) > self.cfg.stream_price_to_beat_max_diff_bps:
            self.new_mismatches.append(slug)
        return obs

    def stats(self) -> PtbValidationStats:
        if self._stats is None:
            self._stats = compute_stats(
                self.observations, max_diff_bps=self.cfg.stream_price_to_beat_max_diff_bps
            )
        return self._stats

    def allowed(self) -> bool:
        if self.synthetic or self.cfg.stream_price_to_beat_policy == "off":
            return False
        return gate(self.stats(), self.cfg)[0]

    def report(self) -> dict[str, Any]:
        stats = self.stats()
        open_, reasons = gate(stats, self.cfg)
        return {
            "PRICE_TO_BEAT_VALIDATION": stats.as_dict(),
            "policy": self.cfg.stream_price_to_beat_policy,
            "required_windows": self.cfg.stream_price_to_beat_min_windows,
            "max_diff_bps": self.cfg.stream_price_to_beat_max_diff_bps,
            "gate_open": open_ and not self.synthetic,
            "gate_closed_because": reasons + (["synthetic session"] if self.synthetic else []),
        }
