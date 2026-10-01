"""Decision-pipeline observability (docs/diagnostics.md).

The diagnostics must (1) explain every NO_TRADE with the first blocking stage,
(2) reproduce the live paper-session symptom "0 trades" when Gamma has not
published the price to beat of the running window, and (3) never change a
decision (they are write-only; the synthetic backtest numbers are unchanged).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from polymarket_bot.app.build import Assembly, assemble
from polymarket_bot.app.cli import EXIT_OK, main
from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.config.loader import load_config
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.types import TradingMode
from polymarket_bot.market.discovery import discovery_slugs
from polymarket_bot.market.hub import MarketDataHub, TrackedMarket
from polymarket_bot.monitoring.pipeline import (
    MAX_REASON_KEYS,
    OVERFLOW_KEY,
    ReasonCounter,
    classify_entry_result,
    explain_zeros,
    funnel,
    no_trade_reason,
    normalize_reason,
    verdict,
)
from polymarket_bot.ports import RawMessage
from polymarket_bot.strategies.btc_5m.fair_value import FairValueEstimate
from polymarket_bot.watchdog.health import HealthRegistry
from tests.factories import make_candidate, make_market, make_snapshot

ROOT = Path(__file__).resolve().parents[2]
CONFIG = str(ROOT / "configs" / "paper.yaml")
FIX = ROOT / "tests" / "fixtures"
D = Decimal
# Real market btc-updown-5m-1790127600 (fixture), same ids as tests/integration/test_hub.py.
T0 = 1790127600_000
UP = "15329049429643092784575279708521316783620398456746213132371567405585638717474"
DOWN = "33215755676580564146338381586841381125623704954041225555979286173231064394422"
COND = "0xc77927db1e825c26dfadd89a4113dd0c4cc2609a2a3f9cb455546662ba074676"
PTB = 86635.83220274656


def _raw(src: str, kind: str, payload: object, t: int) -> RawMessage:
    return RawMessage(src, kind, payload, t, t * 1_000_000)


def _gamma(ptb: float | None = None) -> object:
    markets = json.loads((FIX / "gamma" / "btc_5m_twap60_market_open_1790127600.json").read_text())
    if ptb is not None:
        markets[0]["events"][0]["eventMetadata"] = {"priceToBeat": ptb}
    return markets


def _book(token: str, bid: str, ask: str, ts: int) -> str:
    return json.dumps(
        [
            {
                "event_type": "book",
                "asset_id": token,
                "market": COND,
                "bids": [{"price": bid, "size": "100"}],
                "asks": [{"price": ask, "size": "100"}],
                "timestamp": str(ts),
                "hash": "x",
                "tick_size": "0.01",
            }
        ]
    )


def _rtds(topic: str, ts: int, value: float, window: int | None = None) -> str:
    payload: dict[str, object] = {"symbol": "btc/usd", "timestamp": ts, "value": value}
    if window:
        payload["full_accuracy_value"] = str(int(D(str(value)) * D(10) ** 18))
        payload["window_s"] = window
    return json.dumps({"topic": topic, "type": "update", "timestamp": ts + 50, "payload": payload})


def _feed_running_window(hub: MarketDataHub, now: int, *, official_ptb: float | None) -> None:
    """Healthy books and reference feeds; Gamma price to beat as given."""
    hub.on_raw(_raw("gamma", "markets", _gamma(official_ptb), now))
    hub.on_raw(_raw("clob_ws", "connection", {"state": "connected"}, now))
    hub.on_raw(_raw("rtds", "connection", {"state": "connected"}, now))
    for i in range(25):
        hub.on_raw(_raw("clob_ws", "ws_frame", _book(UP, "0.60", "0.62", now - 100 - i), now - i))
    hub.on_raw(_raw("clob_ws", "ws_frame", _book(DOWN, "0.38", "0.40", now - 100), now))
    hub.on_raw(_raw("clob_ws", "heartbeat", "PONG", now))
    hub.on_raw(_raw("rtds", "ws_frame", _rtds("crypto_prices_twap_sixty", T0, PTB, 60), T0 + 60))
    hub.on_raw(
        _raw("rtds", "ws_frame", _rtds("crypto_prices_chainlink", now - 300, 86700.0), now - 200)
    )
    hub.on_raw(
        _raw(
            "rtds", "ws_frame", _rtds("crypto_prices_twap_sixty", now - 300, 86690.0, 60), now - 200
        )
    )


def _paper_core(tmp_path: Path, now: int) -> Assembly:
    asm = assemble(
        load_config(CONFIG), mode=TradingMode.PAPER, clock=SimulatedClock(now), data_dir=tmp_path
    )
    asm.core.start()
    return asm


# ---------------------------------------------------------------- reasons and counters
def test_reason_normalisation_masks_numbers_and_bounds_keys() -> None:
    assert normalize_reason("Up book stale 2345ms") == "Up book stale #ms"
    assert normalize_reason("conservative edge -0.0123 < 0.03") == "conservative edge # < #"
    assert normalize_reason("") == "unspecified"
    counter = ReasonCounter()
    for i in range(MAX_REASON_KEYS + 5):
        counter.add(f"reason-{chr(65 + i % 26)}{chr(65 + i // 26)}")
    assert len(counter.counts) == MAX_REASON_KEYS + 1
    assert counter.counts[OVERFLOW_KEY] == 5
    assert counter.total() == MAX_REASON_KEYS + 5


def _estimate(ok: bool, *reasons: str) -> FairValueEstimate:
    return FairValueEstimate(ok, 0.7 if ok else 0.5, 0.65, 0.75, "test", tuple(reasons))


def test_no_trade_reason_names_the_first_blocking_stage() -> None:
    passing = make_candidate()
    rejected = make_candidate(outcome="Down", conservative_edge=-0.1, rejections=("no ask",))
    edge_low = make_candidate(conservative_edge=0.01, rejections=("conservative edge 0.01 < 0.03",))
    stale = make_snapshot(stale_reasons=("price to beat not verified: price to beat unknown",))
    fresh = make_snapshot()
    unverified = _estimate(False, "price to beat unknown/unverified")

    assert no_trade_reason(fresh, _estimate(True), [passing, rejected]) is None
    assert no_trade_reason(stale, unverified, [rejected]) == (
        "data: price to beat not verified: price to beat unknown"
    )
    assert no_trade_reason(fresh, _estimate(False, "volatility not ready (3 samples)"), [rejected])
    assert no_trade_reason(fresh, _estimate(True), [rejected, edge_low]) == (
        "edge (Up): conservative edge 0.01 < 0.03"
    )
    assert classify_entry_result("submitted intent-1") is None
    assert classify_entry_result("risk: spread too wide") == "risk: spread too wide"
    assert classify_entry_result("llm review pending") == "llm: llm review pending"


def test_zero_explanations_and_verdicts() -> None:
    feeds = {"messages_by_source_kind": {"clob_ws:ws_frame": 10, "clob_ws:heartbeat": 99}}
    counters = {
        "decisions": 4,
        "features_computed": 4,
        "fair_value_ok": 0,
        "candidates": 8,
        "candidates_rejected": 8,
        "fair_value_reasons": {"price to beat unknown/unverified": 4},
        "candidate_rejections": {"fair value: price to beat unknown/unverified": 8},
        "stale_reasons": {"price to beat not verified: price to beat unknown": 4},
    }
    assert funnel(counters, feeds)["market_updates"] == 10  # heartbeats are not data
    why = explain_zeros(counters, feeds)
    assert "price to beat unknown/unverified (x4)" in why["fair_value_ok"]
    assert "every candidate rejected before the Risk Engine" in why["candidates_passing"]
    assert why["paper_orders"] == "no risk-approved entry"
    assert why["exits"] == "no open position to exit"
    assert verdict(counters, feeds).startswith("INTEGRATION PROBLEM: the price to beat")

    edge_only = {
        **counters,
        "fair_value_ok": 4,
        "stale_reasons": {},
        "snapshots_stale": 0,
        "candidate_rejections": {"conservative edge # < #": 8},
    }
    assert verdict(edge_only, feeds).startswith("NO TRADE EXPECTED with this configuration")
    assert verdict({}, {}).startswith("INTEGRATION PROBLEM: no market data")


# ---------------------------------------------------------------- hub evidence
def test_hub_counts_messages_by_kind_and_rtds_topics() -> None:
    now = T0 + 120_000
    hub = MarketDataHub(AppConfig(), SimulatedClock(now), HealthRegistry())
    _feed_running_window(hub, now, official_ptb=None)
    counts = hub.message_counts
    assert counts["clob_ws:heartbeat"] == 1
    assert counts["clob_ws:ws_frame"] == 26
    assert counts["gamma:markets"] == 1
    rtds = hub.reference.message_counts
    assert rtds["update|crypto_prices_twap_sixty|btc/usd"] == 2
    assert rtds["update|crypto_prices_chainlink|btc/usd"] == 1


def test_official_price_to_beat_is_compared_with_the_stream_when_first_seen() -> None:
    now = T0 + 420_000  # published by Gamma only after the window ended (research §5)
    hub = MarketDataHub(AppConfig(), SimulatedClock(now), HealthRegistry())
    _feed_running_window(hub, now, official_ptb=None)
    assert list(hub.ptb_checks) == []
    hub.on_raw(_raw("gamma", "markets", _gamma(PTB), now))
    hub.on_raw(_raw("gamma", "markets", _gamma(PTB), now + 30_000))  # recorded once
    (check,) = hub.ptb_checks
    assert check["slug"] == "btc-updown-5m-1790127600"
    assert check["diff_bps"] == 0
    assert check["first_seen_after_end_s"] == 120.0


def test_discovery_requeries_ended_markets_until_the_official_price_to_beat_is_known() -> None:
    now = T0 + 420_000
    hub = MarketDataHub(AppConfig(), SimulatedClock(now), HealthRegistry())
    market = make_market()
    hub.markets[COND] = TrackedMarket(market, winner="Up")
    cfg = AppConfig().market_data
    assert market.slug in discovery_slugs(now, hub, cfg)
    hub.markets[COND].official_price_to_beat = D(str(PTB))
    assert market.slug not in discovery_slugs(now, hub, cfg)


# ---------------------------------------------------------------- core funnel
async def test_missing_official_price_to_beat_reproduces_zero_trades(tmp_path: Path) -> None:
    """The live symptom: healthy feeds, running window, no Gamma price to beat."""
    now = T0 + 120_000
    asm = _paper_core(tmp_path, now)
    _feed_running_window(asm.hub, now, official_ptb=None)
    await asm.core.step()

    report = asm.core.pipeline_report(now)
    c = report["counters"]
    assert c["decisions"] == 1 and c["features_computed"] == 1
    assert c["fair_value_ok"] == 0 and c["fair_value_failed"] == 1
    assert c["candidates"] == 2 and c["candidates_rejected"] == 2
    assert c["candidates_passing"] == 0 and c["risk_evaluated"] == 0
    assert c["paper_orders_entry"] == 0
    assert c["fair_value_reasons"]["price to beat unknown/unverified"] == 1
    (reason,) = c["no_trade"]
    assert reason.startswith("data: price to beat not verified")
    assert report["verdict"].startswith("INTEGRATION PROBLEM: the price to beat")

    (diag,) = report["active_markets"]
    assert diag["slug"] == "btc-updown-5m-1790127600"
    assert diag["tokens"] == {"Up": UP, "Down": DOWN}
    assert diag["time_remaining_s"] == 180.0
    assert diag["official_price_to_beat"] is None
    assert diag["price_to_beat"] == {"value": PTB, "source": "rtds", "verified": False}
    assert diag["stream_twap_at_start"]["exact"] == PTB
    assert [b["best_ask"] for b in diag["books"]] == [0.62, 0.40]
    assert diag["no_trade_reason"] == reason
    assert asm.store.read_status("pipeline") is not None  # published for `diagnose`


async def test_verified_price_to_beat_moves_the_blocker_downstream(tmp_path: Path) -> None:
    now = T0 + 120_000
    asm = _paper_core(tmp_path, now)
    _feed_running_window(asm.hub, now, official_ptb=PTB)
    await asm.core.step()
    c = asm.core.pipeline.as_dict(now)
    assert c["snapshots_fresh"] == 1
    # Fresh data; the model still needs 60 s of 1 s returns -> no trade, for that reason.
    assert list(c["no_trade"]) == ["fair value: volatility not ready (# samples)"]


def test_diagnose_cli_is_read_only_and_reports_the_blocker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio  # noqa: PLC0415

    now = T0 + 120_000
    asm = _paper_core(tmp_path, now)
    _feed_running_window(asm.hub, now, official_ptb=None)
    asyncio.run(asm.core.step())
    before = (tmp_path / "audit.jsonl").read_bytes()

    args = ["--config", CONFIG, "--data-dir", str(tmp_path), "diagnose"]
    assert main(args) == EXIT_OK
    text = capsys.readouterr().out
    assert "VERDICT: INTEGRATION PROBLEM" in text
    assert "btc-updown-5m-1790127600" in text
    assert f"Up={UP}" in text
    assert "state_change" in text
    for section in (
        "REFERENCE FEEDS",
        "dispersion final",
        "REFERENCE COUNTERS",
        "LIVENESS",
        "PRICE_TO_BEAT_VALIDATION",
        "N_WINDOWS",
        "HALTS",
    ):
        assert section in text, section

    assert main([*args, "--json"]) == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["pipeline"]["counters"]["decisions"] == 1
    assert report["audit_log"]["kinds"]["core_start"] == 1
    assert (tmp_path / "audit.jsonl").read_bytes() == before  # nothing written


async def test_live_binance_frame_no_longer_trips_the_dispersion_check(tmp_path: Path) -> None:
    """Regression of the 2026-09-25 session: 954/965 decisions were "source dispersion"."""
    now = T0 + 120_000
    asm = _paper_core(tmp_path, now)
    _feed_running_window(asm.hub, now, official_ptb=PTB)
    binance = {
        "topic": "crypto_prices",
        "type": "update",
        "timestamp": now - 250,
        "payload": {
            "symbol": "btcusdt",
            "timestamp": now - 300,
            "value": 86705.12,
            "full_accuracy_value": "86705.12000000",  # plain decimal on this topic
        },
    }
    asm.hub.on_raw(_raw("rtds", "ws_frame", json.dumps(binance), now - 200))
    await asm.core.step()
    c = asm.core.pipeline.as_dict(now)
    assert c["dispersion_rejects"] == 0
    assert c["secondary_valid"] == 1 and c["spot_valid"] == 1 and c["twap_valid"] == 1
    assert c["price_to_beat_verified"] == 1
    ref = asm.core.pipeline_report(now)["reference"]
    assert ref["reference_values"]["secondary"]["value"] == 86705.12
    assert ref["dispersion_final_bps"] == pytest.approx(0.59, abs=0.01)
