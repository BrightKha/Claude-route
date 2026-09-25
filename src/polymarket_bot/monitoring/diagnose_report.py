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


def _reference(ref: dict[str, Any]) -> list[str]:
    vals = ref["reference_values"]
    labels = {"spot": "chainlink spot", "twap60": "twap60", "secondary": "secondary"}
    lines = ["reference values:"]
    for key, label in labels.items():
        v = vals[key]
        raw = v.get("raw") or {}
        lines.append(
            f"{INDENT}{label:<15}= {_fmt(v['value'])}  (age {_fmt(v['age_ms'], ' ms')}, "
            f"{v['source']}, {v['quote']}, topic {v['topic']}; full_accuracy_value is "
            f"{v['full_accuracy_value_encoding']}; raw full_accuracy_value="
            f"{raw.get('full_accuracy_value')!r} value={raw.get('value')!r} "
            f"decoded_as={raw.get('decoded_as')})"
        )
    norm = ref["normalized_values"]
    lines += [
        "normalized values (USD per BTC):",
        f"{INDENT}spot = {_fmt(norm['spot_usd'])}, twap60 = {_fmt(norm['twap60_usd'])}, "
        f"secondary = {_fmt(norm['secondary_usd'])}",
        f"{INDENT}({norm['note']})",
        "dispersion pairs:",
    ]
    for pair, bps in ref["dispersion_pairs_bps"].items():
        lines.append(f"{INDENT}{pair:<17}= {_fmt(bps, ' bps')}")
    lines.append(
        f"dispersion final = {_fmt(ref['dispersion_final_bps'], ' bps')} "
        f"(threshold {_fmt(ref.get('dispersion_threshold_bps'), ' bps')}; "
        f"{ref['dispersion_formula']})"
    )
    for series, example in (ref.get("rejected_example") or {}).items():
        lines.append(f"last rejected {series} tick: {example}")
    stats = ref["stats"]
    lines.append(
        f"frames {stats['frames']} (empty {stats['empty_frames']}, malformed "
        f"{stats['malformed_frames']}, server errors {stats['server_errors']}"
        f"{': ' + str(stats['last_server_error']) if stats['last_server_error'] else ''}), "
        f"outliers {stats['outliers']}"
    )
    for series, st in stats["series"].items():
        lines.append(
            f"{INDENT}{series:<10} updates {st['updates']} history {st['history_points']} "
            f"stored {st['stored_ticks']} rejected {st['rejected'] or 'none'}"
        )
    return lines


def _ptb_validation(val: dict[str, Any] | None) -> list[str]:
    if not val:
        return ["none"]
    stats = val["PRICE_TO_BEAT_VALIDATION"]
    lines = [f"{k:<19}{v}" for k, v in stats.items() if k not in ("MISMATCHES",)]
    lines.append(f"{'MISMATCHES':<19}{stats['MISMATCHES'] or 'none'}")
    lines.append(
        f"policy {val['policy']} | required windows {val['required_windows']} | match "
        f"tolerance {val['max_diff_bps']} bps | gate "
        f"{'OPEN' if val['gate_open'] else 'CLOSED: ' + '; '.join(val['gate_closed_because'])}"
    )
    lines.extend(
        f"{row['window']}  official {row['official_price_to_beat']}  rtds "
        f"{_fmt(row['rtds_twap_at_window_start'])}  diff {_fmt(row['difference_bps'], ' bps')}"
        f"  ({row['source']})"
        for row in val.get("latest") or []
    )
    return lines


def _halts(halts: list[dict[str, Any]]) -> list[str]:
    if not halts:
        return ["none"]
    lines: list[str] = []
    for h in halts:
        lines.append(
            f"HALT {h['halt_at']} {h['from_state']}->{h['to_state']} | category "
            f"{h.get('category')} | component {h.get('component')} | manual {h.get('manual_only')}"
        )
        lines.append(f"{INDENT}cause: {h['cause']}")
        if h.get("condition"):
            lines.append(f"{INDENT}condition: {h['condition']}")
        lines.append(
            f"{INDENT}recovery started {_fmt(h.get('recovery_started_at'))} | recovered "
            f"{_fmt(h.get('recovered_at'))} | downtime {_fmt(h.get('downtime_s'), ' s')}"
        )
    return lines


def format_diagnostic(report: dict[str, Any], *, now_ms: int) -> str:
    pipe = report.get("pipeline")
    out = ["=== DECISION PIPELINE DIAGNOSTIC (read-only) ==="]
    tail = ["", "PRICE_TO_BEAT_VALIDATION (all real observations in the state DB)"]
    tail += [INDENT + line for line in _ptb_validation(report.get("price_to_beat_validation"))]
    tail += ["", "HALTS (audit log; cause, source component, condition, recovery)"]
    tail += [INDENT + line for line in _halts(report.get("halts") or [])]
    if pipe is None:
        out.append("no 'pipeline' status published yet: start `make paper` (this build) first")
        return "\n".join(out + tail)
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
    out += ["", f"VERDICT: {pipe['verdict']}"]
    if pipe.get("reference"):
        out += ["", "REFERENCE FEEDS (inputs of the dispersion check)"]
        out += [INDENT + line for line in _reference(pipe["reference"])]
    rc = feeds.get("reference_counters") or {}
    out += [
        "",
        "REFERENCE COUNTERS",
        f"{INDENT}reference_messages {rc.get('reference_messages')} | spot_updates "
        f"{rc.get('spot_updates')} | twap_updates {rc.get('twap_updates')} | secondary_updates "
        f"{rc.get('secondary_updates')}",
        f"{INDENT}per decision: spot valid/stale/missing {c.get('spot_valid')}/"
        f"{c.get('spot_stale')}/{c.get('spot_missing')} | twap {c.get('twap_valid')}/"
        f"{c.get('twap_stale')}/{c.get('twap_missing')} | secondary {c.get('secondary_valid')}/"
        f"{c.get('secondary_stale')}/{c.get('secondary_missing')}",
        f"{INDENT}dispersion_rejects {c.get('dispersion_rejects')} | price_to_beat_verified "
        f"{c.get('price_to_beat_verified')} | price_to_beat_unverified "
        f"{c.get('price_to_beat_unverified')}",
    ]
    live = pipe.get("liveness")
    if live:
        out += [
            "",
            "LIVENESS (a heartbeat is not data; the watchdog reads book events and ticks)",
            f"{INDENT}market: socket alive {live['market_socket_connected']} | heartbeat "
            f"{_fmt(live['market_heartbeat_age_s'], ' s ago')} | any frame "
            f"{_fmt(live['market_frame_age_s'], ' s ago')} | book events "
            f"{_fmt(live['market_book_event_age_s'], ' s ago')} | price events "
            f"{_fmt(live['market_price_event_age_s'], ' s ago')}",
            f"{INDENT}reference: socket alive {live['reference_socket_connected']} | any frame "
            f"{_fmt(live['reference_frame_age_s'], ' s ago')} | spot/TWAP tick "
            f"{_fmt(live['reference_tick_age_s'], ' s ago')} | per series "
            f"{live['reference_series_age_s']}",
        ]
    out += ["", "RUNNING MARKET(S)"]
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
    out += tail
    audit = report.get("audit_log") or {}
    out += [
        "",
        f"AUDIT LOG: {audit.get('records')} records (valid={audit.get('valid')}) "
        f"kinds={audit.get('kinds')}",
    ]
    for rec in audit.get("latest") or []:
        out.append(f"{INDENT}#{rec['seq']} {rec['at']} {rec['kind']} {rec.get('change', '')}")
    return "\n".join(out)
