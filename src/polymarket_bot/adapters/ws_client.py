"""Resilient WebSocket client used for the CLOB market channel and RTDS.

Unlike the SDK stream managers (which reconnect silently), this client makes
every connection change visible to the consumer as a ``connection`` RawMessage,
so books are invalidated and trading is halted during gaps (fail closed).

Features: exponential backoff with jitter, resubscription on reconnect,
application-level ``PING`` text heartbeat, silence detection, frame recording.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidHandshake, WebSocketException

from polymarket_bot.domain.clock import Clock
from polymarket_bot.ports import RawMessage

log = logging.getLogger(__name__)

HEARTBEAT_REPLIES = frozenset({"PONG", "pong"})


class _Conn(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


ConnectFn = Callable[[str], Any]  # returns an async context manager yielding _Conn


def _default_connect(url: str) -> Any:
    return ws_connect(
        url,
        open_timeout=10,
        close_timeout=5,
        ping_interval=20,  # protocol-level keepalive in addition to app-level PING
        ping_timeout=20,
        max_size=8 * 1024 * 1024,
        max_queue=4096,
    )


class ResilientWebSocket:
    def __init__(
        self,
        *,
        url: str,
        source: str,
        clock: Clock,
        initial_frames: Callable[[], list[str]],
        ping_interval_s: float,
        silence_timeout_s: float,
        reconnect_base_s: float,
        reconnect_max_s: float,
        jitter: float,
        connect: ConnectFn | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._url = url
        self._source = source
        self._clock = clock
        self._initial_frames = initial_frames
        self._ping_interval = ping_interval_s
        self._silence = silence_timeout_s
        self._base = reconnect_base_s
        self._max = reconnect_max_s
        self._jitter = jitter
        self._connect = connect or _default_connect
        self._rng = rng or random.Random()  # noqa: S311 - jitter only
        self._sleep = sleep
        self._ws: _Conn | None = None
        self._closed = False
        self._attempt = 0
        self.connections = 0
        self.disconnections = 0

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def backoff_delay(self, attempt: int) -> float:
        base = min(self._max, self._base * (2**attempt))
        return float(base * (1 + self._jitter * self._rng.random()))

    async def send(self, frame: str) -> bool:
        """Send a (dynamic subscription) frame if connected; False otherwise."""
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(frame)
        except WebSocketException:
            return False
        return True

    def _msg(self, kind: str, payload: Any) -> RawMessage:
        return RawMessage(
            source=self._source,
            kind=kind,
            payload=payload,
            received_ms=self._clock.now_ms(),
            monotonic_ns=self._clock.monotonic_ns(),
        )

    async def stream(self) -> AsyncIterator[RawMessage]:
        while not self._closed:
            reason = "closed"
            try:
                async with self._connect(self._url) as ws:
                    self._ws = ws
                    self._attempt = 0
                    self.connections += 1
                    for frame in self._initial_frames():
                        await ws.send(frame)
                    yield self._msg("connection", {"state": "connected"})
                    pinger = asyncio.create_task(self._ping_loop(ws))
                    try:
                        while not self._closed:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=self._silence)
                            except TimeoutError:
                                reason = f"silence > {self._silence}s"
                                break
                            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                            if text in HEARTBEAT_REPLIES:
                                yield self._msg("heartbeat", text)
                                continue
                            yield self._msg("ws_frame", text)
                    finally:
                        pinger.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await pinger
            except (OSError, WebSocketException, InvalidHandshake, TimeoutError) as exc:
                reason = f"{type(exc).__name__}"
            finally:
                was_connected = self._ws is not None
                self._ws = None
            if self._stopped():  # method call: close() may run concurrently
                break
            if was_connected:
                self.disconnections += 1
            yield self._msg("connection", {"state": "disconnected", "reason": reason})
            delay = self.backoff_delay(self._attempt)
            self._attempt += 1
            log.warning(
                "%s websocket down (%s); reconnecting in %.1fs", self._source, reason, delay
            )
            await self._sleep(delay)

    def _stopped(self) -> bool:
        return self._closed

    async def _ping_loop(self, ws: _Conn) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            try:
                await ws.send("PING")
            except WebSocketException:
                return

    async def close(self) -> None:
        self._closed = True
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
