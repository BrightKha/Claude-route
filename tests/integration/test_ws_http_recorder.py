"""WebSocket resilience (fake server), HTTP retry policy (mock transport), record/replay."""

from __future__ import annotations

import asyncio
import json
import random
from contextlib import asynccontextmanager

import httpx
import pytest
from websockets.exceptions import ConnectionClosedError

from polymarket_bot.adapters.http_base import PublicDataError, PublicHttp
from polymarket_bot.adapters.polymarket_public import ClobPublicData
from polymarket_bot.adapters.ws_client import ResilientWebSocket
from polymarket_bot.config.app_config import MarketDataConfig
from polymarket_bot.data.recorder import SessionRecorder
from polymarket_bot.data.replay import ReplayError, iter_messages, open_session
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.ports import RawMessage


class FakeConn:
    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        if not self.script:
            await asyncio.sleep(3600)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, float):
            await asyncio.sleep(item)
            return await self.recv()
        return item

    async def close(self):
        pass


def fake_connect_factory(connections):
    conns = iter(connections)

    @asynccontextmanager
    async def _connect(url):
        conn = next(conns)
        if isinstance(conn, Exception):
            raise conn
        yield conn

    return _connect


def _ws(connections, silence=0.2):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    ws = ResilientWebSocket(
        url="wss://example.invalid",
        source="clob_ws",
        clock=SimulatedClock(1000),
        initial_frames=lambda: ['{"type":"market","assets_ids":["1"]}'],
        ping_interval_s=10,
        silence_timeout_s=silence,
        reconnect_base_s=1,
        reconnect_max_s=30,
        jitter=0.25,
        connect=fake_connect_factory(connections),
        rng=random.Random(0),
        sleep=fake_sleep,
    )
    return ws, sleeps


async def _collect(ws, n):
    out = []
    async for msg in ws.stream():
        out.append(msg)
        if len(out) >= n:
            break
    await ws.close()
    return out


async def test_ws_reconnects_resubscribes_and_reports_gaps():
    c1 = FakeConn(['{"event_type":"book"}', ConnectionClosedError(None, None)])
    c2 = FakeConn(["PONG", '{"event_type":"price_change"}'])
    ws, sleeps = _ws([c1, c2])
    msgs = await _collect(ws, 6)
    kinds = [(m.kind, m.payload if m.kind == "connection" else None) for m in msgs]
    assert kinds[0] == ("connection", {"state": "connected"})
    assert kinds[1][0] == "ws_frame"
    assert kinds[2][0] == "connection" and kinds[2][1]["state"] == "disconnected"
    assert kinds[3] == ("connection", {"state": "connected"})
    assert kinds[4][0] == "heartbeat"
    assert kinds[5][0] == "ws_frame"
    assert c1.sent[0].startswith('{"type":"market"') and c2.sent[0] == c1.sent[0]
    assert len(sleeps) == 1 and 1.0 <= sleeps[0] <= 1.25


async def test_ws_silence_triggers_reconnect():
    c1 = FakeConn([])  # never sends anything
    c2 = FakeConn(['{"x":1}'])
    ws, _ = _ws([c1, c2], silence=0.05)
    msgs = await _collect(ws, 3)
    assert msgs[1].kind == "connection" and "silence" in msgs[1].payload["reason"]


async def test_ws_connect_failures_back_off_exponentially():
    ws, sleeps = _ws([OSError("refused"), OSError("refused"), OSError("refused"), FakeConn(["{}"])])
    msgs = await _collect(ws, 5)
    assert [m.payload.get("state") for m in msgs if m.kind == "connection"] == [
        "disconnected",
        "disconnected",
        "disconnected",
        "connected",
    ]
    assert sleeps[0] < sleeps[1] < sleeps[2]
    assert ws.backoff_delay(20) <= 30 * 1.25


def _mock_http(handler, retries=2):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def no_sleep(_):
        return None

    return PublicHttp(
        "https://clob.example",
        SimulatedClock(5),
        source="clob_rest",
        timeout_s=1,
        max_retries=retries,
        client=client,
    )


async def test_http_retries_5xx_then_succeeds(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url)
        if len(calls) < 2:
            return httpx.Response(503)
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr(asyncio, "sleep", _instant)
    msg = await _mock_http(handler).get_json("/x", None, kind="x")
    assert msg.payload == {"ok": True} and len(calls) == 2


async def _instant(_s):
    return None


async def test_http_does_not_retry_4xx(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    monkeypatch.setattr(asyncio, "sleep", _instant)
    with pytest.raises(PublicDataError):
        await _mock_http(handler).get_json("/x", None, kind="x")
    assert len(calls) == 1


async def test_http_invalid_json_fails_closed(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _instant)
    with pytest.raises(PublicDataError):
        await _mock_http(lambda r: httpx.Response(200, text="<html>")).get_json(
            "/x", None, kind="x"
        )


async def test_server_time_parsing():
    http = _mock_http(lambda r: httpx.Response(200, text="1790128813"))
    clob = ClobPublicData(MarketDataConfig(), SimulatedClock(0), http=http)
    assert await clob.fetch_server_time_ms() == 1790128813000
    bad = ClobPublicData(
        MarketDataConfig(),
        SimulatedClock(0),
        http=_mock_http(lambda r: httpx.Response(200, text='{"t":1}')),
    )
    with pytest.raises(PublicDataError):
        await bad.fetch_server_time_ms()


# ------------------------------------------------------------------ record / replay
def test_record_then_replay_roundtrip(tmp_path):
    clock = SimulatedClock(1000)
    rec = SessionRecorder(tmp_path, "s1", clock, flush_every=1, synthetic=False)
    msgs = [
        RawMessage("clob_ws", "ws_frame", '{"event_type":"book"}', 1000, 1),
        RawMessage("rtds", "ws_frame", '{"topic":"x"}', 1005, 2),
        RawMessage("clob_ws", "connection", {"state": "disconnected"}, 1010, 3),
    ]
    for m in msgs:
        rec.record_raw(m)
    rec.record_bot("decision", {"allowed": False})
    rec.close()
    session = open_session(tmp_path / "s1")
    assert session.synthetic is False
    replayed = list(iter_messages(session))
    assert [(m.source, m.kind, m.payload, m.received_ms) for m in replayed] == [
        (m.source, m.kind, m.payload, m.received_ms) for m in msgs
    ]
    assert len(list(iter_messages(session, include_bot=True))) == 4


def test_replay_rejects_time_going_backwards(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    lines = [
        {"v": 1, "seq": 1, "src": "clob_ws", "kind": "ws_frame", "t": 2000, "data": "{}"},
        {"v": 1, "seq": 2, "src": "clob_ws", "kind": "ws_frame", "t": 1000, "data": "{}"},
    ]
    (d / "part-0001.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    with pytest.raises(ReplayError, match="backwards"):
        list(iter_messages(open_session(d)))


def test_replay_rejects_corrupt_lines_and_unknown_provenance_is_synthetic(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "part-0001.jsonl").write_text('{"v":1,"seq":1,"src":"clob_ws"\n')
    session = open_session(d)
    assert session.synthetic is True
    with pytest.raises(ReplayError, match="corrupt"):
        list(iter_messages(session))
