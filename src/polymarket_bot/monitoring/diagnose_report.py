"""Human-readable rendering of the ``diagnose`` report (read-only CLI)."""

from __future__ import annotations

from typing import Any

from polymarket_bot.monitoring.pipeline import FUNNEL, funnel

INDENT = "  "


def _counter(title: str, counts: dict[str, int] | None, limit: int = 8) -> list[str]:
    if not counts:
        return [f"{title}: none"]
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [f"{title}:"] + [f"{INDENT}{n:>8}  {reason}" for reason, n in items]


def _fmt(value: Any, suffix: str = "") -> str:
    return "-" if value is None else f"{value}{suffix}"


def _market(diag: dict[str, Any]) -> list[str]:
    lines = [
        f"slug {diag['slug']} | market id {diag['market_id']} | condition {diag['condition_id']}",
        "tokens: " + ", ".join(f"{o}={t}" for o, t in diag["tokens"].items()),
        f"window {diag['window_start']} -> {diag['window_end']} | elapsed "
        f"{diag['time_since_start_s']}s | remaining {diag['time_remaining_s']}s | "
        f"accepting orders: {diag['accepting_orders']}",
    ]
    for b in diag.get("books") or []:
        state = "valid" if b["book_valid"] else f"INVALID ({b['invalid_reason']})"
        lines.append(
            f"{b['outcome']:<5} bid {_fmt(b['best_bid'])} ask {_fmt(b['best_ask'])} "
            f"spread {_fmt(b['spread'])} depth(bid/ask) {_fmt(b['bid_depth_usd'])}/"
            f"{_fmt(b['ask_depth_usd'])} USD | book age {_fmt(b['book_age_ms'], ' ms')} | {state}"
        )
    ref = diag.get("reference")
    if ref:
        lines.append(
            f"reference: chainlink spot {_fmt(ref['spot'])} (age {_fmt(ref['spot_age_ms'], ' ms')})"
            f" | twap60 {_fmt(ref['twap60'])} (age {_fmt(ref['twap60_age_ms'], ' ms')})"
            f" | secondary {_fmt(ref['secondary'])} (age {_fmt(ref['secondary_age_ms'], ' ms')})"
            f" | dispersion {_fmt(ref['dispersion_bps'], ' bps')}"
        )
    ptb = diag.get("price_to_beat") or {}
    stream = diag.get("stream_twap_at_start") or {}
    lines.append(
        f"price to beat: {_fmt(ptb.get('value'))} source={ptb.get('source')} "
        f"verified={ptb.get('verified')} | official (Gamma): "
        f"{_fmt(diag.get('official_price_to_beat'))} | RTDS TWAP at start: exact "
        f"{_fmt(stream.get('exact'))} (nearest tick before "
        f"{_fmt(stream.get('nearest_before_offset_ms'), ' ms')}, after "
        f"{_fmt(stream.get('nearest_after_offset_ms'), ' ms')})"
    )
    res = diag.get("resolution") or {}
    lines.append(
        f"resolution: rule {diag['rule_id']} validated={res.get('rule_validated')} "
        f"consistent={res.get('consistent')} winner={_fmt(res.get('winner'))} | gamma refreshed "
        f"{_fmt(diag.get('gamma_last_refresh_age_s'), ' s')} ago"
    )
    stale = diag.get("stale_reasons")
    lines.append("stale reasons: " + ("; ".join(stale) if stale else "none (fresh)"))
    fv = diag.get("fair_value")
    if fv:
        lines.append(
            f"fair value: p_up={fv['p_up']} band={fv['band']}"
            if fv["ok"]
            else "fair value: NOT OK: " + "; ".join(fv["reasons"])
        )
    for c in diag.get("candidates") or []:
        verdict = "PASSES" if c["passes"] else "rejected: " + "; ".join(c["rejections"])
        lines.append(
            f"candidate {c['outcome']}: price {_fmt(c['executable_price'])} fair "
            f"{c['fair_probability']} conservative edge {c['conservative_edge']} -> {verdict}"
        )
    reason = diag.get("no_trade_reason")
    lines.append(f"=> {'TRADE submitted' if reason is None else 'NO_TRADE: ' + reason}")
    return lines


def format_diagnostic(report: dict[str, Any], *, now_ms: int) -> str:
    pipe = report.get("pipeline")
    out = ["=== DECISION PIPELINE DIAGNOSTIC (read-only) ==="]
    if pipe is None:
        out.append("no 'pipeline' status published yet: start `make paper` (this build) first")
        return "\n".join(out)
    age = (now_ms - report["published_ms"]) / 1000 if report.get("published_ms") else None
    c = pipe["counters"]
    feeds = pipe["feeds"]
    out.append(
        f"published {_fmt(None if age is None else round(age, 1), ' s')} ago | mode "
        f"{pipe['mode']} | state {pipe['state']} ({pipe['state_reason']}) | llm reviewer: "
        f"{pipe['llm_reviewer']} | uptime {c['uptime_s']} s"
    )
    out += ["", "PIPELINE (since start; one decision = one running market x one step)"]
    f = funnel(c, feeds)
    why = pipe.get("why_zero", {})
    for key, label in FUNNEL:
        note = f"   <- {why[key]}" if key in why else ""
        out.append(f"{INDENT}{label + ' ':.<24} {f[key]:>8}{note}")
    out.append(
        f"{INDENT}(steps {c['entry_passes']}, without running market "
        f"{c['steps_without_active_market']}, snapshots fresh/stale {c['snapshots_fresh']}/"
        f"{c['snapshots_stale']}, blocked by bot state {c['candidates_blocked_by_state']}, "
        f"execution refused {c['execution_refused']}, fills entry/exit {c['fills_entry']}/"
        f"{c['fills_exit']})"
    )
    out.append("")
    out += _counter("NO_TRADE reasons (first blocking stage, per decision)", c["no_trade"])
    out += _counter("stale-snapshot reasons (all, per decision)", c["stale_reasons"])
    out += _counter("fair-value failure reasons", c["fair_value_reasons"])
    out += _counter("candidate rejections (all, per candidate)", c["candidate_rejections"])
    out += _counter("bot-state blocks", c["blocked_states"])
    out += _counter("llm verdicts", c["llm_verdicts"])
    out += _counter("risk rejections", c["risk_reasons"])
    out += ["", f"VERDICT: {pipe['verdict']}", "", "RUNNING MARKET(S)"]
    markets = pipe.get("active_markets") or []
    if not markets:
        out.append(f"{INDENT}none")
    for diag in markets:
        out += [INDENT + line for line in _market(diag)]
    out += ["", "TRACKED MARKETS (selection by rolling slug btc-updown-5m-<window start>)"]
    for row in pipe.get("tracked_markets") or []:
        out.append(
            f"{INDENT}{row['phase']:<28} {row['slug']} start {row['window_start']} accepting="
            f"{row['accepting_orders']} official_ptb={_fmt(row['official_price_to_beat'])} "
            f"winner={_fmt(row['winner'])}"
        )
    out += ["", "FEEDS"]
    out.append(f"{INDENT}messages by source:kind: {feeds['messages_by_source_kind']}")
    out.append(
        f"{INDENT}connected: market ws={feeds['market_ws_connected']} "
        f"rtds={feeds['rtds_connected']} | clock drift {_fmt(feeds['clock_drift_ms'], ' ms')}"
    )
    out.append(f"{INDENT}book events: {feeds['book_events']}")
    out.append(
        f"{INDENT}rtds messages by type|topic|symbol: {feeds['rtds_messages_by_type_topic_symbol']}"
    )
    out.append(
        f"{INDENT}rtds ticks stored: {feeds['rtds_ticks_stored']} | malformed "
        f"{feeds['rtds_malformed']} | outliers {feeds['rtds_outliers']}"
    )
    out.append(
        f"{INDENT}markets rejected by validation: {feeds['markets_rejected_by_validation']} "
        f"{feeds['market_rejection_reasons'] or ''}"
    )
    out += ["", "PRICE-TO-BEAT CHECKS (official Gamma value when first seen vs RTDS TWAP)"]
    checks = pipe.get("price_to_beat_checks") or []
    if not checks:
        out.append(f"{INDENT}none yet (Gamma publishes it only after the window; see docs)")
    for chk in checks:
        out.append(
            f"{INDENT}{chk['slug']} official {chk['official']} stream "
            f"{_fmt(chk['stream_twap_exact'])}"
            f" diff {_fmt(chk['diff_bps'], ' bps')} | first seen {chk['first_seen_after_end_s']} s"
            " after window end"
        )
    audit = report.get("audit_log") or {}
    out += [
        "",
        f"AUDIT LOG: {audit.get('records')} records (valid={audit.get('valid')}) "
        f"kinds={audit.get('kinds')}",
    ]
    for rec in audit.get("latest") or []:
        out.append(f"{INDENT}#{rec['seq']} {rec['at']} {rec['kind']} {rec.get('change', '')}")
    return "\n".join(out)
