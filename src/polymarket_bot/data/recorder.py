"""Session recorder: every raw message and every bot decision, as JSON lines.

Envelope: ``{"v":1,"seq":n,"src":..,"kind":..,"t":recv_ms,"mono":ns,"data":..,"meta":..}``.
Market-data sources are what the replay engine re-emits; ``src == "bot"``
records (decisions, orders, fills, state) are for audit/comparison only.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from polymarket_bot.audit.jsonable import to_jsonable
from polymarket_bot.domain.clock import Clock
from polymarket_bot.ports import RawMessage
from polymarket_bot.security.redaction import redact_obj

FORMAT_VERSION = 1
MARKET_DATA_SOURCES = frozenset({"gamma", "clob_rest", "clob_ws", "rtds", "synthetic"})


class SessionRecorder:
    def __init__(
        self,
        directory: Path,
        session_id: str,
        clock: Clock,
        *,
        max_bytes: int = 256 * 1024 * 1024,
        flush_every: int = 200,
        synthetic: bool = False,
    ) -> None:
        self.directory = directory / session_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._max_bytes = max_bytes
        self._flush_every = flush_every
        self._lock = threading.Lock()
        self._seq = 0
        self._part = 0
        self._written = 0
        self._fh = self._open_part()
        meta = {"format": FORMAT_VERSION, "session_id": session_id, "synthetic": synthetic}
        (self.directory / "session.json").write_text(json.dumps(meta), encoding="utf-8")

    def _open_part(self) -> Any:
        self._part += 1
        self._written = 0
        path = self.directory / f"part-{self._part:04d}.jsonl"
        return path.open("a", encoding="utf-8")

    def record_raw(self, msg: RawMessage) -> None:
        self._write(
            msg.source,
            msg.kind,
            t=msg.received_ms,
            mono=msg.monotonic_ns,
            data=msg.payload,
            meta=msg.meta,
        )

    def record_bot(self, kind: str, payload: Any) -> None:
        self._write(
            "bot",
            kind,
            t=self._clock.now_ms(),
            mono=self._clock.monotonic_ns(),
            data=payload,
            meta=None,
        )

    def _write(self, src: str, kind: str, *, t: int, mono: int, data: Any, meta: Any) -> None:
        with self._lock:
            self._seq += 1
            envelope = {
                "v": FORMAT_VERSION,
                "seq": self._seq,
                "src": src,
                "kind": kind,
                "t": t,
                "mono": mono,
                "data": redact_obj(to_jsonable(data)) if src == "bot" else data,
                "meta": to_jsonable(meta) if meta else None,
            }
            line = json.dumps(envelope, separators=(",", ":"), default=str) + "\n"
            self._fh.write(line)
            self._written += len(line)
            if self._seq % self._flush_every == 0:
                self._fh.flush()
            if self._written >= self._max_bytes:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()
                self._fh = self._open_part()

    def close(self) -> None:
        with self._lock:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
