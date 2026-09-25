"""Decision-pipeline observability: funnel counters and per-market diagnostics.

Answers "why did the bot not trade?" (docs/diagnostics.md). Everything here is
write-only from the trading core's point of view: no decision ever reads a
counter or a diagnostic, so adding or changing them cannot change what the bot
trades. Documents are published in the ``pipeline`` status document and read by
``polymarket_bot.app diagnose``.

Reasons are normalised (numbers masked) so that counters stay bounded.
Only public market data is stored here — never a secret or an account field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from polymarket_bot.domain.decisions import TradeCandidate
    from polymarket_bot.domain.snapshot import MarketSnapshot
    from polymarket_bot.market.hub import MarketDataHub, TrackedMarket
    from polymarket_bot.strategies.btc_5m.fair_value import FairValueEstimate

MAX_REASON_KEYS = 40
REASON_KEY_LEN = 90
OVERFLOW_KEY = "(other reasons)"
TOP_N = 8
_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?")


def normalize_reason(reason: str) -> str:
    """Bounded counter key: numbers masked as ``#``, length capped."""
    text = _NUMBER.sub("#", reason.strip())
    return text[:REASON_KEY_LEN] or "unspecified"


def iso_ms(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _num(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


class ReasonCounter:
    """Counter keyed by normalised reason, with a bounded number of keys."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def add(self, reason: str, n: int = 1) -> None:
        key = normalize_reason(reason)
        if key not in self.counts and len(self.counts) >= MAX_REASON_KEYS:
            key = OVERFLOW_KEY
        self.counts[key] = self.counts.get(key, 0) + n

    def total(self) -> int:
        return sum(self.counts.values())

    def top(self, n: int = TOP_N) -> list[tuple[str, int]]:
        return sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


@dataclass
class PipelineCounters:
    """Funnel counters since the core started (one decision = one market x one step)."""

    started_ms: int
    entry_passes: int = 0  # decision steps that ran the entry pass
    steps_without_active_market: int = 0
    decisions: int = 0
    snapshots_missing: int = 0
    snapshots_fresh: int = 0
    snapshots_stale: int = 0
    features_computed: int = 0
    fair_value_ok: int = 0
    fair_value_failed: int = 0
    candidates: int = 0
    candidates_passing: int = 0
    candidates_rejected: int = 0
    candidates_blocked_by_state: int = 0
    llm_checked: int = 0
    risk_evaluated: int = 0
    risk_approved: int = 0
    risk_rejected: int = 0
    execution_refused: int = 0
    paper_orders_entry: int = 0
    paper_orders_exit: int = 0
    fills_entry: int = 0
    fills_exit: int = 0
    positions_evaluated_for_exit: int = 0
    exit_signals: int = 0
    exit_risk_rejected: int = 0
    trades_decided: int = 0  # decisions that ended in a submitted entry order
    stale_reasons: ReasonCounter = field(default_factory=ReasonCounter)
    fair_value_reasons: ReasonCounter = field(default_factory=ReasonCounter)
    candidate_rejections: ReasonCounter = field(default_factory=ReasonCounter)
    blocked_states: ReasonCounter = field(default_factory=ReasonCounter)
    llm_verdicts: ReasonCounter = field(default_factory=ReasonCounter)
    risk_reasons: ReasonCounter = field(default_factory=ReasonCounter)
    no_trade: ReasonCounter = field(default_factory=ReasonCounter)

    def observe_evaluation(
        self,
        snap: MarketSnapshot,
        estimate: FairValueEstimate,
        candidates: list[TradeCandidate],
    ) -> None:
        self.features_computed += 1
        if snap.is_fresh:
            self.snapshots_fresh += 1
        else:
            self.snapshots_stale += 1
            for reason in snap.stale_reasons:
                self.stale_reasons.add(reason)
        if estimate.ok:
            self.fair_value_ok += 1
        else:
            self.fair_value_failed += 1
            for reason in estimate.reasons:
                self.fair_value_reasons.add(reason)
        for cand in candidates:
            self.candidates += 1
            if cand.passes_filters:
                self.candidates_passing += 1
            else:
                self.candidates_rejected += 1
                for reason in cand.rejections:
                    self.candidate_rejections.add(reason)

    def as_dict(self, now_ms: int) -> dict[str, Any]:
        out: dict[str, Any] = {"started_ms": self.started_ms, "uptime_s": _uptime(self, now_ms)}
        for name, value in vars(self).items():
            if isinstance(value, ReasonCounter):
                out[name] = dict(value.top(MAX_REASON_KEYS))
            elif name != "started_ms":
                out[name] = value
        return out


def _uptime(pc: PipelineCounters, now_ms: int) -> float:
    return round(max(0, now_ms - pc.started_ms) / 1000, 1)


def no_trade_reason(
    snap: MarketSnapshot, estimate: FairValueEstimate, candidates: list[TradeCandidate]
) -> str | None:
    """First blocking stage before the Risk Engine (None when a candidate passes)."""
    if any(c.passes_filters for c in candidates):
        return None
    if not snap.is_fresh:
        return f"data: {snap.stale_reasons[0]}"
    if not estimate.ok:
        return f"fair value: {estimate.reasons[0] if estimate.reasons else 'not ok'}"
    best = max(candidates, key=lambda c: c.conservative_edge, default=None)
    if best is None or not best.rejections:
        return "edge: no candidate"
    return f"edge ({best.outcome}): {best.rejections[0]}"


def classify_entry_result(result: str) -> str | None:
    """Map a ``TradingCore._try_entry`` result to a NO_TRADE reason (None = submitted)."""
    if result.startswith("submitted"):
        return None
    if result.startswith(("risk:", "execution")):
        return result
    return f"llm: {result}"


# ---------------------------------------------------------------------- per-market diagnostics
def market_diagnostic(
    hub: MarketDataHub,
    tracked: TrackedMarket,
    *,
    snap: MarketSnapshot | None,
    estimate: FairValueEstimate | None,
    candidates: list[TradeCandidate],
    no_trade: str | None,
    now_ms: int,
) -> dict[str, Any]:
    d = tracked.definition
    out: dict[str, Any] = {
        "slug": d.slug,
        "market_id": d.market_id,
        "condition_id": d.condition_id,
        "rule_id": d.rule_id,
        "tokens": {t.outcome: t.token_id for t in d.tokens},
        "window_start": iso_ms(d.window_start_ms),
        "window_end": iso_ms(d.window_end_ms),
        "time_since_start_s": round((now_ms - d.window_start_ms) / 1000, 1),
        "time_remaining_s": round((d.window_end_ms - now_ms) / 1000, 1),
        "accepting_orders": d.accepting_orders,
        "resolution": {
            "rule_validated": True,  # only validated markets are ever tracked
            "consistent": tracked.resolution_consistent,
            "winner": tracked.winner,
        },
        "official_price_to_beat": _num(tracked.official_price_to_beat),
        "gamma_last_refresh_age_s": (
            round((now_ms - tracked.last_refresh_ms) / 1000, 1) if tracked.last_refresh_ms else None
        ),
        "stream_twap_at_start": stream_twap_at(hub, d.window_start_ms),
        "no_trade_reason": no_trade,
    }
    if snap is None:
        out["snapshot"] = None
        return out
    ref = snap.reference
    out["books"] = [
        {
            "outcome": q.outcome,
            "best_bid": _num(q.best_bid),
            "best_ask": _num(q.best_ask),
            "spread": _num(q.spread),
            "bid_depth_usd": _num(q.bid_depth_usd),
            "ask_depth_usd": _num(q.ask_depth_usd),
            "book_age_ms": q.book_age_ms,
            "book_valid": q.book_valid,
            "invalid_reason": None if q.book_valid else _invalid_reason(hub, q.token_id),
        }
        for q in snap.quotes
    ]
    out["reference"] = {
        "spot": _num(ref.spot),
        "spot_age_ms": ref.spot_age_ms,
        "twap60": _num(ref.twap),
        "twap60_age_ms": ref.twap_age_ms,
        "secondary": _num(ref.secondary_spot),
        "secondary_age_ms": ref.secondary_age_ms,
        "dispersion_bps": None if ref.dispersion_bps is None else round(ref.dispersion_bps, 2),
    }
    out["price_to_beat"] = {
        "value": _num(ref.price_to_beat),
        "source": ref.price_to_beat_source,
        "verified": ref.price_to_beat_verified,
    }
    out["stale_reasons"] = list(snap.stale_reasons)
    if estimate is not None:
        out["fair_value"] = {
            "ok": estimate.ok,
            "p_up": round(estimate.p_up, 4) if estimate.ok else None,
            "band": [round(estimate.p_lower, 4), round(estimate.p_upper, 4)]
            if estimate.ok
            else None,
            "reasons": list(estimate.reasons),
        }
    out["candidates"] = [
        {
            "outcome": c.outcome,
            "executable_price": _num(c.executable_price),
            "fair_probability": round(c.fair_probability, 4),
            "conservative_edge": round(c.conservative_edge, 4),
            "passes": c.passes_filters,
            "rejections": list(c.rejections[:4]),
        }
        for c in candidates
    ]
    return out


def _invalid_reason(hub: MarketDataHub, token_id: str) -> str | None:
    book = hub.books.get(token_id)
    return book.invalid_reason if book is not None else "untracked"


def stream_twap_at(hub: MarketDataHub, window_start_ms: int) -> dict[str, Any]:
    """RTDS 60 s TWAP ticks around a window start (the unverified price-to-beat source)."""
    series = hub.reference.twap60
    exact = series.exact(window_start_ms)
    before = series.at_or_before(window_start_ms)
    after = next(iter(series.window(window_start_ms + 1, window_start_ms + 60_000)), None)
    return {
        "exact": _num(exact.value) if exact else None,
        "nearest_before_offset_ms": (before.observed_ms - window_start_ms) if before else None,
        "nearest_after_offset_ms": (after.observed_ms - window_start_ms) if after else None,
    }


def tracked_markets(hub: MarketDataHub, now_ms: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tm in sorted(hub.markets.values(), key=lambda t: t.definition.window_start_ms):
        d = tm.definition
        if d.window_end_ms < now_ms - 30 * 60_000:
            continue
        if now_ms < d.window_start_ms:
            phase = "upcoming"
        elif now_ms < d.window_end_ms:
            phase = "running"
        else:
            phase = "resolved" if tm.winner else "ended (awaiting resolution)"
        rows.append(
            {
                "slug": d.slug,
                "phase": phase,
                "window_start": iso_ms(d.window_start_ms),
                "accepting_orders": d.accepting_orders,
                "official_price_to_beat": _num(tm.official_price_to_beat),
                "winner": tm.winner,
            }
        )
    return rows


def feed_diagnostics(hub: MarketDataHub) -> dict[str, Any]:
    ref = hub.reference
    a = hub.apply_stats
    return {
        "messages_by_source_kind": dict(sorted(hub.message_counts.items())),
        "market_ws_connected": hub.market_ws_connected,
        "rtds_connected": hub.rtds_connected,
        "book_events": {
            "applied": a.applied,
            "ignored_unknown_event": a.ignored_unknown_event,
            "malformed": a.malformed,
            "untracked_asset": a.untracked,
        },
        "clob_frames": hub.stats.frames,
        "clob_malformed_frames": hub.stats.malformed_frames,
        "rtds_messages_by_type_topic_symbol": dict(sorted(ref.message_counts.items())),
        "rtds_ticks_stored": {
            "spot": len(ref.spot),
            "twap60": len(ref.twap60),
            "secondary": len(ref.secondary),
        },
        "rtds_malformed": ref.malformed,
        "rtds_outliers": ref.outliers,
        "clock_drift_ms": hub.drift.estimate_ms(),
        "markets_rejected_by_validation": hub.stats.rejected_markets,
        "market_rejection_reasons": dict(hub.stats.rejection_reasons or {}),
    }


# ---------------------------------------------------------------------- explanations
FUNNEL: tuple[tuple[str, str], ...] = (
    ("market_updates", "market updates"),
    ("decisions", "decisions"),
    ("features_computed", "features computed"),
    ("fair_value_ok", "fair value ok"),
    ("candidates", "candidates"),
    ("candidates_rejected", "candidates rejected"),
    ("candidates_passing", "candidates passing"),
    ("risk_evaluated", "reached Risk Engine"),
    ("risk_approved", "risk approved"),
    ("risk_rejected", "risk rejected"),
    ("paper_orders", "paper orders"),
    ("paper_fills", "paper fills"),
    ("exits", "exits submitted"),
)


def funnel(counters: dict[str, Any], feeds: dict[str, Any]) -> dict[str, int]:
    messages = feeds.get("messages_by_source_kind", {})
    data_msgs = sum(v for k, v in messages.items() if not k.endswith((":connection", ":heartbeat")))
    c = counters
    return {
        "market_updates": int(data_msgs),
        "decisions": int(c.get("decisions", 0)),
        "features_computed": int(c.get("features_computed", 0)),
        "fair_value_ok": int(c.get("fair_value_ok", 0)),
        "candidates": int(c.get("candidates", 0)),
        "candidates_rejected": int(c.get("candidates_rejected", 0)),
        "candidates_passing": int(c.get("candidates_passing", 0)),
        "risk_evaluated": int(c.get("risk_evaluated", 0)),
        "risk_approved": int(c.get("risk_approved", 0)),
        "risk_rejected": int(c.get("risk_rejected", 0)),
        "paper_orders": int(c.get("paper_orders_entry", 0)) + int(c.get("paper_orders_exit", 0)),
        "paper_fills": int(c.get("fills_entry", 0)) + int(c.get("fills_exit", 0)),
        "exits": int(c.get("paper_orders_exit", 0)),
    }


def _top(counter: dict[str, int], n: int = 3) -> str:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return "; ".join(f"{k} (x{v})" for k, v in items) or "no reason recorded"


def explain_zeros(counters: dict[str, Any], feeds: dict[str, Any]) -> dict[str, str]:
    """Why each zero stage of the funnel is zero, from the counters only."""
    f = funnel(counters, feeds)
    c = counters
    why: dict[str, str] = {}
    if f["market_updates"] == 0:
        why["market_updates"] = "no market data received (network, websocket or discovery)"
    if f["decisions"] == 0:
        why["decisions"] = (
            "no BTC 5m window running in the tracked markets "
            f"(steps without active market: {c.get('steps_without_active_market', 0)}; "
            f"markets rejected by validation: {feeds.get('markets_rejected_by_validation', 0)})"
        )
    elif f["features_computed"] == 0:
        why["features_computed"] = f"no market snapshot ({c.get('snapshots_missing', 0)} missing)"
    if f["features_computed"] and f["fair_value_ok"] == 0:
        why["fair_value_ok"] = "fair value never computable: " + _top(
            c.get("fair_value_reasons", {})
        )
    if f["candidates"] and f["candidates_passing"] == 0:
        # First blocking stage, not the (tied) list of every rejection of every candidate.
        why["candidates_passing"] = (
            "every candidate rejected before the Risk Engine; first blocking stage: "
            + _top(c.get("no_trade", {}))
        )
    if f["candidates_passing"] and f["risk_evaluated"] == 0:
        blocked = c.get("candidates_blocked_by_state", 0)
        why["risk_evaluated"] = (
            f"passing candidates never reached the Risk Engine: blocked by bot state x{blocked} "
            f"({_top(c.get('blocked_states', {}))}); llm verdicts: "
            f"{_top(c.get('llm_verdicts', {}))}"
        )
    elif f["candidates_passing"] == 0:
        why["risk_evaluated"] = "no candidate passed the edge filters (see candidates_passing)"
    if f["risk_evaluated"] and f["risk_approved"] == 0:
        why["risk_approved"] = "Risk Engine rejected every entry: " + _top(
            c.get("risk_reasons", {})
        )
    if f["paper_orders"] == 0:
        why["paper_orders"] = (
            "no risk-approved entry"
            if f["risk_approved"] == 0
            else f"execution refused x{c.get('execution_refused', 0)}"
        )
    if f["paper_fills"] == 0:
        why["paper_fills"] = (
            "no order submitted"
            if f["paper_orders"] == 0
            else "FAK orders found no liquidity at the limit after latency"
        )
    if f["exits"] == 0:
        why["exits"] = (
            "no open position to exit"
            if c.get("positions_evaluated_for_exit", 0) == 0
            else f"no exit signal ({c.get('exit_signals', 0)} signals)"
        )
    return why


def verdict(counters: dict[str, Any], feeds: dict[str, Any]) -> str:
    """One-line conclusion for the operator (heuristic over the counters)."""
    f = funnel(counters, feeds)
    decisions = f["decisions"]
    if f["market_updates"] == 0 or decisions == 0:
        return "INTEGRATION PROBLEM: no market data or no running market evaluated"
    ptb = sum(
        v for k, v in counters.get("stale_reasons", {}).items() if k.startswith("price to beat")
    )
    if f["fair_value_ok"] == 0 and ptb >= decisions:
        return (
            "INTEGRATION PROBLEM: the price to beat was never verified during a running window, "
            "so fair value is never computed and every decision is NO_TRADE"
        )
    if f["fair_value_ok"] == 0:
        return "INTEGRATION PROBLEM: fair value never computable (see fair_value_reasons)"
    stale = int(counters.get("snapshots_stale", 0))
    if stale >= decisions:
        return "DATA PROBLEM: every snapshot was stale (see stale_reasons)"
    if f["candidates_passing"] == 0:
        return (
            "NO TRADE EXPECTED with this configuration: data valid, but no candidate cleared "
            "the edge filters (see candidate_rejections)"
        )
    if f["risk_approved"] == 0:
        return "candidates passed but none was approved (see blocked_states / llm / risk)"
    return "pipeline active: orders were submitted"


# ---------------------------------------------------------------------- summaries
def summary_line(counters: dict[str, Any], feeds: dict[str, Any]) -> str:
    f = funnel(counters, feeds)
    parts = " ".join(f"{k}={v}" for k, v in f.items())
    return f"pipeline: {parts} | top NO_TRADE: {_top(counters.get('no_trade', {}), 2)}"


def market_line(diag: dict[str, Any]) -> str:
    books = " ".join(
        f"{b['outcome']} {b['best_bid']}/{b['best_ask']} age={b['book_age_ms']}ms"
        for b in diag.get("books", [])
    )
    ptb = diag.get("price_to_beat") or {}
    return (
        f"market {diag['slug']} remaining={diag['time_remaining_s']}s {books} "
        f"ptb={ptb.get('value')} ({ptb.get('source')}, verified={ptb.get('verified')}) "
        f"-> {'TRADE' if diag.get('no_trade_reason') is None else 'NO_TRADE'}: "
        f"{diag.get('no_trade_reason')}"
    )
