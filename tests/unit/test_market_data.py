"""Order book integrity, CLOB message parsing, reference prices, clock drift."""

from __future__ import annotations

import json
import math
import random
from decimal import Decimal
from pathlib import Path

import pytest

from polymarket_bot.data.clock_drift import ClockDriftEstimator
from polymarket_bot.data.reference_prices import ReferencePriceState
from polymarket_bot.market.clob_messages import (
    ApplyStats,
    MessageError,
    apply_market_event,
    apply_rest_book,
    build_market_subscribe,
    build_market_update,
    split_frame,
)
from polymarket_bot.market.orderbook import OrderBookState

D = Decimal
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "clob"
UP = "15329049429643092784575279708521316783620398456746213132371567405585638717474"


def _real_book():
    payload = json.loads((FIX / "book_btc5m_up_1790127600.json").read_text())
    book = OrderBookState(UP)
    apply_rest_book(payload, book, received_ms=1790127240200)
    return book, payload


def test_real_rest_book_is_sorted_correctly():
    book, payload = _real_book()
    snap = book.snapshot()
    assert snap is not None
    # REST lists bids ascending / asks descending: the best levels are the LAST elements.
    assert snap.best_bid == D(payload["bids"][-1]["price"]) == D("0.5")
    assert snap.best_ask == D(payload["asks"][-1]["price"]) == D("0.51")
    assert snap.spread == D("0.01")
    assert snap.tick_size == D("0.01")
    assert snap.exchange_ms == 1790127240090
    assert all(a.price < b.price for a, b in zip(snap.asks, snap.asks[1:], strict=False))
    assert all(a.price > b.price for a, b in zip(snap.bids, snap.bids[1:], strict=False))


def test_rest_book_for_wrong_token_rejected():
    _, payload = _real_book()
    with pytest.raises(MessageError):
        apply_rest_book(payload, OrderBookState("999"), 1)


def _ws_book_event(bids, asks, ts="1000"):
    return {
        "event_type": "book",
        "asset_id": UP,
        "market": "0xc",
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
        "timestamp": ts,
        "hash": "h1",
    }


def _pc(price, size, side, bb, ba, ts="1001"):
    return {
        "event_type": "price_change",
        "market": "0xc",
        "timestamp": ts,
        "price_changes": [
            {
                "asset_id": UP,
                "price": price,
                "size": size,
                "side": side,
                "best_bid": bb,
                "best_ask": ba,
            }
        ],
    }


def _books():
    return {UP: OrderBookState(UP)}


def test_deltas_apply_and_zero_size_removes_level():
    books, stats = _books(), ApplyStats()
    apply_market_event(_ws_book_event([(".48", "30")], [(".52", "25")]), books, 1, stats)
    apply_market_event(_pc("0.49", "10", "BUY", "0.49", "0.52"), books, 2, stats)
    assert books[UP].snapshot().best_bid == D("0.49")
    apply_market_event(_pc("0.49", "0", "BUY", "0.48", "0.52", ts="1002"), books, 3, stats)
    assert books[UP].snapshot().best_bid == D("0.48")
    assert stats.malformed == 0 and stats.applied == 3


def test_deltas_before_snapshot_are_ignored():
    books, stats = _books(), ApplyStats()
    apply_market_event(_pc("0.49", "10", "BUY", "0.49", "0.52"), books, 2, stats)
    assert books[UP].snapshot() is None


def test_best_bid_echo_mismatch_invalidates_book():
    books, stats = _books(), ApplyStats()
    apply_market_event(_ws_book_event([(".48", "30")], [(".52", "25")]), books, 1, stats)
    apply_market_event(_pc("0.49", "10", "BUY", "0.47", "0.52"), books, 2, stats)
    assert books[UP].snapshot() is None
    assert "mismatch" in books[UP].invalid_reason


def test_crossed_book_invalidated():
    books, stats = _books(), ApplyStats()
    apply_market_event(_ws_book_event([(".48", "30")], [(".52", "25")]), books, 1, stats)
    apply_market_event(_pc("0.53", "10", "BUY", None, None), books, 2, stats)
    assert books[UP].snapshot() is None
    assert "crossed" in books[UP].invalid_reason


def test_out_of_order_delta_invalidates():
    books, stats = _books(), ApplyStats()
    apply_market_event(_ws_book_event([(".48", "30")], [(".52", "25")], ts="5000"), books, 1, stats)
    apply_market_event(_pc("0.49", "10", "BUY", "0.49", "0.52", ts="4000"), books, 2, stats)
    assert books[UP].snapshot() is None


def test_malformed_event_invalidates_tracked_book():
    books, stats = _books(), ApplyStats()
    apply_market_event(_ws_book_event([(".48", "30")], [(".52", "25")]), books, 1, stats)
    bad = _ws_book_event([(".48", "abc")], [(".52", "25")])
    apply_market_event(bad, books, 2, stats)
    assert stats.malformed == 1
    assert books[UP].snapshot() is None


def test_duplicate_snapshot_is_idempotent():
    books, stats = _books(), ApplyStats()
    ev = _ws_book_event([(".48", "30")], [(".52", "25")])
    apply_market_event(ev, books, 1, stats)
    s1 = books[UP].snapshot()
    apply_market_event(ev, books, 2, stats)
    s2 = books[UP].snapshot()
    assert s1.bids == s2.bids and s1.asks == s2.asks


def test_tick_size_change_and_unknown_events():
    books, stats = _books(), ApplyStats()
    apply_market_event(_ws_book_event([(".48", "30")], [(".52", "25")]), books, 1, stats)
    apply_market_event(
        {
            "event_type": "tick_size_change",
            "asset_id": UP,
            "old_tick_size": "0.01",
            "new_tick_size": "0.001",
        },
        books,
        2,
        stats,
    )
    assert books[UP].tick_size == D("0.001")
    apply_market_event({"event_type": "something_new"}, books, 3, stats)
    assert stats.ignored_unknown_event == 1
    apply_market_event(
        {"event_type": "tick_size_change", "asset_id": UP, "new_tick_size": "0.02"}, books, 4, stats
    )
    assert books[UP].snapshot() is None  # unsupported tick => invalid


def test_market_resolved_event_collected():
    stats = ApplyStats()
    apply_market_event(
        {"event_type": "market_resolved", "market": "0xc", "winning_asset_id": UP},
        _books(),
        1,
        stats,
    )
    assert stats.resolved_markets == [("0xc", UP)]


def test_frames_may_be_arrays_and_non_json_rejected():
    assert len(split_frame('[{"event_type":"book"},{"event_type":"book"}]')) == 2
    with pytest.raises(MessageError):
        split_frame("PONGX")
    with pytest.raises(MessageError):
        split_frame("[1,2]")


def test_subscription_frames():
    assert json.loads(build_market_subscribe(["b", "a"])) == {
        "type": "market",
        "assets_ids": ["a", "b"],
        "custom_feature_enabled": True,
    }
    assert json.loads(build_market_update(["x"], subscribe=False)) == {
        "operation": "unsubscribe",
        "assets_ids": ["x"],
    }


# ------------------------------------------------------------------ reference prices
def _rtds(topic, ts, value, symbol="btc/usd", full=False, window=None):
    payload = {"symbol": symbol, "timestamp": ts, "value": value}
    if full:
        payload["full_accuracy_value"] = str(int(D(str(value)) * D(10) ** 18))
        payload["window_s"] = window
    return json.dumps({"topic": topic, "type": "update", "timestamp": ts + 100, "payload": payload})


def test_twap_full_accuracy_and_price_to_beat_verification():
    ref = ReferencePriceState()
    start = 1790127600_000
    ref.on_frame(
        _rtds("crypto_prices_twap_sixty", start, 86635.83220274656, full=True, window=60),
        start + 150,
    )
    p = ref.price_to_beat(start, D("86635.83220274656"), tolerance_bps=2)
    assert p.verified and p.source == "gamma+rtds"
    conflict = ref.price_to_beat(start, D("86735"), tolerance_bps=2)
    assert not conflict.verified and conflict.value is None
    assert ref.price_to_beat(start, None, tolerance_bps=2).verified is False
    assert ref.price_to_beat(start + 300_000, None, tolerance_bps=2).source == "none"


def test_wrong_twap_window_rejected():
    ref = ReferencePriceState()
    assert (
        ref.on_frame(_rtds("crypto_prices_twap_sixty", 1000, 86000.0, full=True, window=30), 1100)
        == 0
    )
    assert ref.malformed == 1


def test_spot_outlier_rejected_and_marks_suspect():
    ref = ReferencePriceState()
    assert ref.on_frame(_rtds("crypto_prices_chainlink", 1000, 86000.0), 1100) == 1
    assert ref.on_frame(_rtds("crypto_prices_chainlink", 2000, 95000.0), 2100) == 0
    assert ref.outliers == 1 and ref.is_suspect(2200)
    assert ref.spot.latest().value == D("86000.0")


def test_out_of_order_and_duplicate_ticks_ignored():
    ref = ReferencePriceState()
    ref.on_frame(_rtds("crypto_prices_chainlink", 2000, 86000.0), 2100)
    assert ref.on_frame(_rtds("crypto_prices_chainlink", 2000, 86001.0), 2200) == 0
    assert ref.on_frame(_rtds("crypto_prices_chainlink", 1000, 86002.0), 2300) == 0


def test_other_symbols_and_malformed_ignored():
    ref = ReferencePriceState()
    assert ref.on_frame(_rtds("crypto_prices_chainlink", 1000, 3000.0, symbol="eth/usd"), 1100) == 0
    assert ref.on_frame("not json", 1) == 0
    assert (
        ref.on_frame(
            json.dumps({"topic": "crypto_prices_chainlink", "type": "update", "payload": {}}), 1
        )
        == 0
    )
    assert ref.malformed == 2


def test_trailing_average_piecewise_constant():
    ref = ReferencePriceState()
    ref.on_frame(_rtds("crypto_prices_chainlink", 0, 100.0), 10)
    ref.on_frame(_rtds("crypto_prices_chainlink", 30_000, 101.0), 30_010)
    avg, coverage = ref.trailing_average(0, 60_000)
    assert avg == D("100.5") and coverage == 1.0
    _avg2, cov2 = ref.trailing_average(-30_000, 60_000)
    assert cov2 < 1.0


def test_realized_vol_estimator_tracks_known_volatility():
    rng = random.Random(1)
    ref = ReferencePriceState(vol_halflife_s=120)
    price = 86000.0
    sigma = 2e-4  # per sqrt(second)
    for s in range(1, 1200):
        price *= math.exp(rng.gauss(0, sigma))
        ref.on_frame(_rtds("crypto_prices_chainlink", s * 1000, price), s * 1000 + 50)
    vol, n = ref.vol_per_sqrt_s()
    assert n > 1000
    assert 0.6 * sigma < vol < 1.4 * sigma


def test_clock_drift_estimator():
    est = ClockDriftEstimator(min_samples=5)
    assert est.estimate_ms() is None
    for i in range(10):
        est.add(exchange_ms=1000 * i, received_ms=1000 * i + 120)
    assert est.estimate_ms() == 120
