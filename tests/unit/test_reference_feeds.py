"""Reference-price feeds: per-topic units, Decimal exactness, history, PolyBolt, liveness.

Root cause pinned here (docs/research.md §4): the legacy ``crypto_prices``
(Binance) topic sends ``full_accuracy_value`` as a *plain decimal string*, the
Chainlink topics as an E18 integer. The old parser divided every
``full_accuracy_value`` by 1e18, turning 84186.07 into 8.418607e-14 and the
spot/secondary dispersion into 414462.93 bps (every decision NO_TRADE).
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any

import pytest

from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.data.polybolt import (
    PolyBoltSequencer,
    build_subscribe_frame,
    parse_frame,
)
from polymarket_bot.data.reference_prices import (
    E18,
    ReferencePriceState,
    UnitError,
    decode_exact,
    parse_point,
)
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.market.hub import MarketDataHub
from polymarket_bot.market.orderbook import OrderBookState
from polymarket_bot.ports import RawMessage
from polymarket_bot.watchdog.health import HealthRegistry

D = Decimal

# Verbatim frames captured on wss://ws-live-data.polymarket.com on 2026-09-05
# (polyoxide design note, docs/research.md §4): same keys, ~1 s apart, different scale.
CAPTURED_BINANCE = (
    '{"connection_id":"gZexFa6cUWeIKEiTDA==","payload":{"full_accuracy_value":"79697.73000000",'
    '"symbol":"btcusdt","timestamp":1788600389000,"value":79697.73},"timestamp":1788600389154,'
    '"topic":"crypto_prices","type":"update"}'
)
CAPTURED_CHAINLINK = (
    '{"connection_id":"gZexFa6cUWeIKEiTDA==","payload":{"full_accuracy_value":'
    '"79696948174287960000000","symbol":"btc/usd","timestamp":1788600388000,'
    '"value":79696.94817428796},"timestamp":1788600389451,"topic":"crypto_prices_chainlink",'
    '"type":"update"}'
)
CAPTURED_TWAP60 = (
    '{"connection_id":"gZexFa6cUWeIKEiTDA==","payload":{"full_accuracy_value":'
    '"79697575317474428059648","symbol":"btc/usd","timestamp":1788600388000,'
    '"value":79697.57531747443,"window_s":60},"timestamp":1788600389495,'
    '"topic":"crypto_prices_twap_sixty","type":"update"}'
)
CAPTURED_BINANCE_BACKFILL = (
    '{"payload":{"data":[{"timestamp":1788600269000,"value":79697.73},'
    '{"timestamp":1788600270000,"value":79697.73},{"timestamp":1788600271000,"value":79697.73}],'
    '"symbol":"btcusdt"},"timestamp":1788600388752,"topic":"crypto_prices","type":"subscribe"}'
)
RECV = 1788600390000


def _frame(topic: str, ts: int, value: float, *, full: str | None = None, **extra: Any) -> str:
    symbol = "btcusdt" if topic == "crypto_prices" else "btc/usd"
    payload: dict[str, Any] = {"symbol": symbol, "timestamp": ts, "value": value, **extra}
    if full is not None:
        payload["full_accuracy_value"] = full
    return json.dumps({"topic": topic, "type": "update", "timestamp": ts + 50, "payload": payload})


def _e18(value: str) -> str:
    return str(int(D(value) * E18))


# ---------------------------------------------------------------- the root cause
def test_before_after_secondary_scale_on_the_observed_values() -> None:
    """The user's live numbers: spot 84166.00011090243, secondary shown as 8.418607e-14."""
    spot_raw = "84166.00011090243"
    binance_full = "84186.07000000"
    ref = ReferencePriceState()
    ref.on_frame(
        _frame("crypto_prices_chainlink", 1000, float(spot_raw), full=_e18(spot_raw)), 1100
    )
    ref.on_frame(_frame("crypto_prices", 1000, 84186.07, full=binance_full), 1100)

    # BEFORE: what the old parser computed from the very same payload.
    old_secondary = D(binance_full) / E18
    old_dispersion = abs(math.log(float(spot_raw) / float(old_secondary))) * 1e4
    assert f"{float(old_secondary):.7g}" == "8.418607e-14"
    assert round(old_dispersion, 2) == 414462.93

    # AFTER: Binance's field is decoded as the plain decimal it is.
    assert ref.secondary.latest().value == D("84186.07000000")
    diag = ref.diagnostics(1200)
    assert diag["reference_values"]["secondary"]["raw"] == {
        "full_accuracy_value": "84186.07000000",
        "value": "84186.07",
        "decoded_as": "decimal",
    }
    assert diag["reference_values"]["spot"]["raw"]["decoded_as"] == "e18"
    assert diag["dispersion_final_bps"] == pytest.approx(2.384, abs=1e-3)
    assert diag["dispersion_pairs_bps"]["spot/secondary"] == diag["dispersion_final_bps"]


def test_captured_frames_same_field_two_scales() -> None:
    """Differential: identical keys, the two decodings differ by exactly 1e18."""
    ref = ReferencePriceState()
    assert ref.on_frame(CAPTURED_CHAINLINK, RECV) == 1
    assert ref.on_frame(CAPTURED_BINANCE, RECV) == 1
    assert ref.on_frame(CAPTURED_TWAP60, RECV) == 1
    assert ref.spot.latest().value == D("79696.94817428796")
    assert ref.secondary.latest().value == D("79697.73000000")  # not 0.00000000000007969773
    assert ref.twap60.latest().value == D("79697.575317474428059648")  # exact E18, no float
    assert decode_exact("79697", "e18") * E18 == decode_exact("79697", "decimal")
    assert ref.dispersion_bps() == pytest.approx(0.098, abs=1e-3)


def test_unit_mismatch_is_rejected_never_rescaled() -> None:
    """If Binance ever switched to E18, the value cross-check refuses the tick."""
    ref = ReferencePriceState()
    frame = _frame("crypto_prices", 1000, 84186.07, full=_e18("84186.07"))
    assert ref.on_frame(frame, 1100) == 0
    assert ref.secondary.latest() is None
    assert ref.series_stats["secondary"].rejected == {"unit_mismatch": 1}
    example = ref.diagnostics(1200)["rejected_example"]["secondary"]
    assert example["full_accuracy_value"] == "84186070000000000000000"
    with pytest.raises(UnitError):
        parse_point({"timestamp": 1, "value": 1.0, "full_accuracy_value": "1000"}, "decimal")


def test_e18_topics_require_an_integer_string() -> None:
    ref = ReferencePriceState()
    assert ref.on_frame(_frame("crypto_prices_twap_sixty", 1000, 86000.0, full="86000.0"), 1) == 0
    assert ref.series_stats["twap60"].rejected == {"malformed": 1}


def test_values_are_decimal_not_float() -> None:
    ref = ReferencePriceState()
    ref.on_frame(_frame("crypto_prices", 1000, 0.1, full="0.10000000"), 1100)
    value = ref.secondary.latest().value
    assert isinstance(value, Decimal) and value == D("0.10000000")


# ---------------------------------------------------------------- symbols, errors, backfill
def test_symbols_are_case_insensitive_and_others_counted() -> None:
    ref = ReferencePriceState()
    upper = json.loads(_frame("crypto_prices", 1000, 84186.07, full="84186.07"))
    upper["payload"]["symbol"] = "BTCUSDT"
    assert ref.on_frame(json.dumps(upper), 1100) == 1
    eth = json.loads(_frame("crypto_prices", 2000, 3000.0, full="3000.0"))
    eth["payload"]["symbol"] = "ethusdt"
    assert ref.on_frame(json.dumps(eth), 2100) == 0
    assert ref.series_stats["secondary"].rejected == {"wrong_symbol": 1}
    assert ref.message_counts["update|crypto_prices|ethusdt"] == 1


def test_server_error_envelope_and_empty_frame() -> None:
    ref = ReferencePriceState()
    err = {"body": {"message": "leger GetTopics error: topic not found"}, "statusCode": 401}
    assert ref.on_frame(json.dumps(err), 1) == 0
    assert ref.on_frame("", 2) == 0
    assert ref.server_errors == 1 and "not found" in str(ref.last_server_error)
    assert ref.empty_frames == 1 and ref.malformed == 0


def test_subscribe_backfill_seeds_history_but_is_not_live_data() -> None:
    ref = ReferencePriceState()
    assert ref.on_frame(CAPTURED_BINANCE_BACKFILL, RECV) == 0  # no live tick
    assert len(ref.secondary) == 3
    assert ref.series_stats["secondary"].history_points == 3
    assert ref.series_stats["secondary"].updates == 0
    assert "secondary" not in ref.last_live_ms
    twap_backfill = {
        "topic": "crypto_prices_twap_sixty",
        "type": "subscribe",
        "timestamp": RECV,
        "payload": {
            "symbol": "btc/usd",
            "window_s": 60,
            "data": [
                {"timestamp": 1000, "value": 86000.5, "full_accuracy_value": _e18("86000.5")},
                {"timestamp": RECV + 5_000, "value": 86001.0},  # future point: never stored
            ],
        },
    }
    ref.on_frame(json.dumps(twap_backfill), RECV)
    assert ref.twap60.latest().value == D("86000.5") and len(ref.twap60) == 1
    assert ref.twap60.latest().live is False


def test_missing_secondary_gives_no_dispersion() -> None:
    ref = ReferencePriceState()
    ref.on_frame(_frame("crypto_prices_chainlink", 1000, 86000.0), 1100)
    diag = ref.diagnostics(1200)
    assert ref.dispersion_bps() is None and diag["dispersion_final_bps"] is None
    assert diag["dispersion_pairs_bps"]["spot/secondary"] is None
    assert diag["reference_values"]["secondary"]["value"] is None


# ---------------------------------------------------------------- PolyBolt (new topics)
def _pb(**frame: Any) -> str:
    base: dict[str, Any] = {"v": 1, "channel": "price.crypto.twap", "seq": 1, "ts": 5_000}
    return json.dumps({**base, **frame})


def test_polybolt_snapshot_then_update_uses_exact_decimal_without_e18() -> None:
    snap_text = _pb(
        snapshot=True,
        payload={
            "symbol": "btcusd",
            "window_seconds": 60,
            "data": [
                {"timestamp": 1000, "value": "78788.525642908795142144"},
                {
                    "timestamp": 2000,
                    "value": 78788.78657993881,
                    "full_accuracy_value": "78788.786579938813673472",
                },
            ],
        },
    )
    snap, why = parse_frame(snap_text, symbol="btcusd")
    assert snap is not None and why == "" and snap.snapshot
    assert snap.points[1] == (2000, D("78788.786579938813673472"))
    update, _ = parse_frame(
        _pb(
            seq=2,
            payload={
                "symbol": "btcusd",
                "timestamp": 3000,
                "value": "78803.715261094101516288",
                "window_seconds": 60,
            },
        ),
        symbol="btcusd",
    )
    assert update is not None and not update.snapshot
    ref = ReferencePriceState(symbol="btcusd")
    assert ref.apply_polybolt(snap, 5_000) == 0  # history only
    assert ref.apply_polybolt(update, 5_000) == 1  # live
    assert ref.twap60.latest().value == D("78803.715261094101516288")
    assert ref.series_stats["twap60"].history_points == 2


def test_polybolt_rejects_bad_frames_and_e18_values() -> None:
    assert parse_frame('{"op":"authed","rid":"a1"}', symbol="btcusd") == (None, "control:authed")
    assert parse_frame(_pb(v=2, payload={}), symbol="btcusd")[1].startswith("unsupported")
    assert parse_frame(_pb(channel="price.equity", payload={}), symbol="btcusd")[1].startswith(
        "unknown channel"
    )
    no_window = _pb(payload={"symbol": "btcusd", "timestamp": 1, "value": "1"})
    assert parse_frame(no_window, symbol="btcusd")[1] == "window_seconds must be 60"
    e18 = _pb(
        payload={
            "symbol": "btcusd",
            "timestamp": 1,
            "value": "78803.7",
            "full_accuracy_value": _e18("78803.7"),
            "window_seconds": 60,
        }
    )
    frame, why = parse_frame(e18, symbol="btcusd")
    assert frame is None and why.startswith("unit mismatch")


def test_polybolt_wrong_symbol_and_pyth_role() -> None:
    frame, _ = parse_frame(
        _pb(
            channel="price.crypto",
            payload={"symbol": "ETHUSD", "timestamp": 1, "value": "3000.1"},
        ),
        symbol="btcusd",
    )
    assert frame is not None and frame.series == "secondary" and frame.symbol == "ethusd"
    ref = ReferencePriceState(symbol="btcusd")
    assert ref.apply_polybolt(frame, 10) == 0
    assert ref.series_stats["secondary"].rejected == {"wrong_symbol": 1}


def test_polybolt_sequence_gaps_dropped_and_reconnect_reset() -> None:
    seq = PolyBoltSequencer()

    def frame(n: int, dropped: int | None = None) -> Any:
        extra = {"dropped": dropped} if dropped is not None else {}
        payload = {"symbol": "btcusd", "timestamp": n, "value": "1", "window_seconds": 60}
        return parse_frame(_pb(seq=n, payload=payload, **extra), symbol="btcusd")[0]

    assert seq.observe(frame(1)) == []
    assert seq.observe(frame(2)) == []
    assert seq.observe(frame(5, dropped=2)) == ["gap 2->5", "dropped 2"]
    assert seq.gaps == 2 and seq.dropped == 2
    seq.reset()  # reconnect: seq restarts per channel
    assert seq.observe(frame(1)) == []
    assert seq.observe(frame(1)) == ["seq regression 1->1"]


def test_polybolt_subscribe_frame_uses_json_object_filters() -> None:
    frame = json.loads(build_subscribe_frame("btcusd", rid="s1", with_spot=True))
    assert frame == {
        "op": "subscribe",
        "rid": "s1",
        "subscriptions": [
            {"channel": "price.crypto.twap", "filter": {"symbol": "btcusd", "window_seconds": 60}},
            {"channel": "price.crypto", "filter": {"symbol": "btcusd"}},
        ],
    }
    assert "auth" not in build_subscribe_frame("btcusd", rid="s2")  # never handles credentials


# ---------------------------------------------------------------- hub: liveness and reconnect
def _raw(src: str, kind: str, payload: object, t: int) -> RawMessage:
    return RawMessage(src, kind, payload, t, t * 1_000_000)


def test_hub_separates_heartbeat_book_price_and_reference_liveness() -> None:
    health = HealthRegistry()
    hub = MarketDataHub(AppConfig(), SimulatedClock(10_000), health)
    hub.books["tok"] = OrderBookState("tok")
    hub.on_raw(_raw("clob_ws", "connection", {"state": "connected"}, 1_000))
    hub.on_raw(_raw("clob_ws", "heartbeat", "PONG", 2_000))
    snap = health.snapshot(2_000)
    assert snap.market_last_heartbeat_ms == 2_000 and snap.market_last_msg_ms == 2_000
    assert snap.market_last_book_event_ms is None  # a heartbeat is not data
    trade = [{"event_type": "last_trade_price", "asset_id": "tok", "price": "0.5"}]
    hub.on_raw(_raw("clob_ws", "ws_frame", json.dumps(trade), 3_000))
    snap = health.snapshot(3_000)
    assert snap.market_last_price_event_ms == 3_000 and snap.market_last_book_event_ms is None
    book = [
        {
            "event_type": "book",
            "asset_id": "tok",
            "bids": [{"price": "0.4", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
        }
    ]
    hub.on_raw(_raw("clob_ws", "ws_frame", json.dumps(book), 4_000))
    assert health.snapshot(4_000).market_last_book_event_ms == 4_000
    hub.on_raw(_raw("rtds", "connection", {"state": "connected"}, 4_000))
    hub.on_raw(_raw("rtds", "ws_frame", _frame("crypto_prices", 4_500, 86000.0), 5_000))
    snap = health.snapshot(5_000)
    assert snap.reference_series_last_ms == {"secondary": 5_000}
    assert snap.reference_last_event_ms is None  # the cross-check alone does not count
    hub.on_raw(_raw("rtds", "ws_frame", CAPTURED_BINANCE_BACKFILL, 6_000))
    assert health.snapshot(6_000).reference_last_event_ms is None  # backfill is not live
    hub.on_raw(_raw("rtds", "ws_frame", _frame("crypto_prices_chainlink", 6_500, 86000.0), 7_000))
    assert health.snapshot(7_000).reference_last_event_ms == 7_000


def test_rtds_reconnect_marks_reference_disconnected_then_backfill_fills_the_gap() -> None:
    clock = SimulatedClock(10_000)
    hub = MarketDataHub(AppConfig(), clock, HealthRegistry())
    hub.on_raw(_raw("rtds", "connection", {"state": "connected"}, 1_000))
    hub.on_raw(_raw("rtds", "ws_frame", _frame("crypto_prices_chainlink", 1_000, 86000.0), 1_100))
    hub.on_raw(_raw("rtds", "connection", {"state": "disconnected"}, 2_000))
    assert hub.rtds_connected is False
    hub.on_raw(_raw("rtds", "connection", {"state": "connected"}, 9_000))
    backfill = {
        "topic": "crypto_prices_chainlink",
        "type": "subscribe",
        "timestamp": 9_000,
        "payload": {
            "symbol": "btc/usd",
            "data": [{"timestamp": t, "value": 86000.0 + t / 1e6} for t in (2_000, 5_000, 8_000)],
        },
    }
    hub.on_raw(_raw("rtds", "ws_frame", json.dumps(backfill), 9_000))
    assert [t.observed_ms for t in hub.reference.spot.window(0, 10_000)] == [
        1_000,
        2_000,
        5_000,
        8_000,
    ]
    assert hub.reference.series_stats["spot"].history_points == 3
