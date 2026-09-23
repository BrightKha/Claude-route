"""MarketDataHub end-to-end on real payload shapes: discovery -> books -> reference -> snapshot."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.market.discovery import discovery_slugs
from polymarket_bot.market.hub import MarketDataHub
from polymarket_bot.ports import RawMessage
from polymarket_bot.watchdog.health import HealthRegistry

D = Decimal
FIX = Path(__file__).resolve().parents[1] / "fixtures"
T0 = 1790127600_000
UP = "15329049429643092784575279708521316783620398456746213132371567405585638717474"
DOWN = "33215755676580564146338381586841381125623704954041225555979286173231064394422"
COND = "0xc77927db1e825c26dfadd89a4113dd0c4cc2609a2a3f9cb455546662ba074676"


def _raw(src, kind, payload, t):
    return RawMessage(src, kind, payload, t, t * 1_000_000)


def _market_payload_with_ptb(ptb=None):
    markets = json.loads((FIX / "gamma" / "btc_5m_twap60_market_open_1790127600.json").read_text())
    if ptb is not None:
        markets[0]["events"][0]["eventMetadata"] = {"priceToBeat": ptb}
    return markets


def _book_frame(token, bid, ask, ts):
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


def _rtds(topic, ts, value, window=None):
    payload = {"symbol": "btc/usd", "timestamp": ts, "value": value}
    if window:
        payload["full_accuracy_value"] = str(int(D(str(value)) * D(10) ** 18))
        payload["window_s"] = window
    return json.dumps({"topic": topic, "type": "update", "timestamp": ts + 50, "payload": payload})


def _hub(now):
    clock = SimulatedClock(now)
    hub = MarketDataHub(AppConfig(), clock, HealthRegistry())
    return hub, clock


def _feed_healthy(hub, clock, ptb_value=86635.83220274656):
    now = clock.now_ms()
    new = hub.on_raw(_raw("gamma", "markets", _market_payload_with_ptb(ptb_value), now))
    assert set(new) == {UP, DOWN}
    hub.on_raw(_raw("clob_ws", "connection", {"state": "connected"}, now))
    hub.on_raw(_raw("rtds", "connection", {"state": "connected"}, now))
    for i in range(25):
        hub.on_raw(
            _raw("clob_ws", "ws_frame", _book_frame(UP, "0.60", "0.62", now - 100 - i), now - i)
        )
    hub.on_raw(_raw("clob_ws", "ws_frame", _book_frame(DOWN, "0.38", "0.40", now - 100), now))
    hub.on_raw(
        _raw("rtds", "ws_frame", _rtds("crypto_prices_twap_sixty", T0, ptb_value, 60), T0 + 60)
    )
    hub.on_raw(
        _raw("rtds", "ws_frame", _rtds("crypto_prices_chainlink", now - 300, 86700.0), now - 200)
    )
    hub.on_raw(
        _raw(
            "rtds", "ws_frame", _rtds("crypto_prices_twap_sixty", now - 300, 86690.0, 60), now - 200
        )
    )


def test_healthy_snapshot_has_no_stale_reasons():
    hub, clock = _hub(T0 + 120_000)
    _feed_healthy(hub, clock)
    snap = hub.snapshot(COND)
    assert snap is not None
    assert snap.stale_reasons == (), snap.stale_reasons
    assert snap.is_fresh
    assert snap.quote("Up").best_ask == D("0.62")
    assert snap.reference.price_to_beat_verified
    assert snap.reference.price_to_beat_source == "gamma+rtds"
    assert snap.time_to_expiry_ms == 180_000
    assert hub.drift.estimate_ms() == 100


def test_disconnect_invalidates_books_and_snapshot():
    hub, clock = _hub(T0 + 120_000)
    _feed_healthy(hub, clock)
    hub.on_raw(
        _raw("clob_ws", "connection", {"state": "disconnected", "reason": "x"}, clock.now_ms())
    )
    snap = hub.snapshot(COND)
    assert not snap.is_fresh
    assert any("book invalid" in r for r in snap.stale_reasons)
    assert "market websocket disconnected" in snap.stale_reasons
    assert hub.drift.estimate_ms() is None


def test_missing_official_price_to_beat_blocks_after_start():
    hub, clock = _hub(T0 + 120_000)
    now = clock.now_ms()
    hub.on_raw(_raw("gamma", "markets", _market_payload_with_ptb(None), now))
    snap = hub.snapshot(COND)
    assert any("price to beat not verified" in r for r in snap.stale_reasons)


def test_conflicting_price_to_beat_blocks():
    hub, clock = _hub(T0 + 120_000)
    _feed_healthy(hub, clock)
    hub.on_raw(_raw("gamma", "markets", _market_payload_with_ptb(90000.0), clock.now_ms()))
    snap = hub.snapshot(COND)
    assert not snap.reference.price_to_beat_verified
    assert any("differ" in r for r in snap.stale_reasons)


def test_stale_book_and_reference_are_reported():
    hub, clock = _hub(T0 + 120_000)
    _feed_healthy(hub, clock)
    clock.advance_to(clock.now_ms() + 30_000)
    reasons = hub.snapshot(COND).stale_reasons
    assert any("book stale" in r for r in reasons)
    assert any("reference stale" in r for r in reasons)


def test_rejected_markets_counted_not_tracked():
    hub, _ = _hub(T0)
    zombie = json.loads((FIX / "gamma" / "btc_5m_zombie_event_2025-12-19.json").read_text())
    assert hub.on_raw(_raw("gamma", "events", zombie, T0)) == []
    assert hub.markets == {}
    assert hub.stats.rejected_markets == 1


def test_closed_markets_are_never_tracked_for_trading():
    hub, _clock = _hub(1790126700_000)
    events = json.loads((FIX / "gamma" / "btc_5m_twap_events_2026-09-23.json").read_text())
    hub.on_raw(_raw("gamma", "events", events, 1790126700_000))
    assert hub.markets == {}


def test_tracked_market_settlement_recorded_when_resolved():
    hub, clock = _hub(T0 + 120_000)
    _feed_healthy(hub, clock)
    resolved = _market_payload_with_ptb(86635.83220274656)
    resolved[0].update(closed=True, umaResolutionStatus="resolved", outcomePrices='["0", "1"]')
    resolved[0]["events"][0]["eventMetadata"]["finalPrice"] = 86600.0
    hub.on_raw(_raw("gamma", "markets", resolved, T0 + 400_000))
    tracked = hub.markets[COND]
    assert tracked.winner == "Down" and tracked.resolution_consistent
    assert tracked.definition.accepting_orders is False


def test_settlement_contradicting_rule_raises_anomaly():
    hub, clock = _hub(T0 + 120_000)
    _feed_healthy(hub, clock)
    resolved = _market_payload_with_ptb(86635.83220274656)
    resolved[0].update(closed=True, umaResolutionStatus="resolved", outcomePrices='["1", "0"]')
    resolved[0]["events"][0]["eventMetadata"]["finalPrice"] = 86600.0  # rule says Down
    hub.on_raw(_raw("gamma", "markets", resolved, T0 + 400_000))
    assert not hub.markets[COND].resolution_consistent
    assert hub.resolution_anomalies


def test_discovery_slugs_cover_current_and_upcoming_windows():
    hub, clock = _hub(T0 + 42_000)
    slugs = discovery_slugs(clock.now_ms(), hub, AppConfig().market_data)
    assert slugs[0] == "btc-updown-5m-1790127600"
    assert "btc-updown-5m-1790128200" in slugs
    assert len(slugs) == len(set(slugs))


def test_tick_size_change_propagates_to_snapshot_market():
    hub, clock = _hub(T0 + 120_000)
    _feed_healthy(hub, clock)
    frame = json.dumps(
        {
            "event_type": "tick_size_change",
            "asset_id": UP,
            "old_tick_size": "0.01",
            "new_tick_size": "0.001",
        }
    )
    hub.on_raw(_raw("clob_ws", "ws_frame", frame, clock.now_ms()))
    snap = hub.snapshot(COND)
    assert snap.market.tick_size == D("0.01")  # coarser grid of the two tokens
