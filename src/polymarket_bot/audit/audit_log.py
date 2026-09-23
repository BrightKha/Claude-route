"""Append-only, hash-chained audit log (JSON lines).

Each record carries ``prev_hash`` and its own ``hash`` (SHA-256 of the
canonical record without ``hash``). Any edit, deletion or reordering is
detected by :func:`verify_audit_log`. Payloads are redacted before writing.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymarket_bot.audit.jsonable import canonical_dumps, to_jsonable
from polymarket_bot.domain.clock import Clock
from polymarket_bot.security.redaction import redact_obj

GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditRecord:
    seq: int
    ts_ms: int
    kind: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str


def _hash_record(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_dumps(body).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path, clock: Clock, *, fsync: bool = True) -> None:
        self._path = path
        self._clock = clock
        self._fsync = fsync
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._seq, self._prev = self._load_tail()

    def _load_tail(self) -> tuple[int, str]:
        if not self._path.exists():
            return 0, GENESIS_HASH
        last: str | None = None
        with self._path.open("rb") as fh:
            for raw in fh:
                if raw.strip():
                    last = raw.decode("utf-8")
        if last is None:
            return 0, GENESIS_HASH
        record = json.loads(last)
        return int(record["seq"]), str(record["hash"])

    def append(self, kind: str, payload: dict[str, Any]) -> AuditRecord:
        with self._lock:
            body = {
                "seq": self._seq + 1,
                "ts_ms": self._clock.now_ms(),
                "kind": kind,
                "payload": redact_obj(to_jsonable(payload)),
                "prev_hash": self._prev,
            }
            digest = _hash_record(body)
            line = canonical_dumps({**body, "hash": digest})
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                if self._fsync:
                    os.fsync(fh.fileno())
            self._seq += 1
            self._prev = digest
            return AuditRecord(
                body["seq"], body["ts_ms"], kind, body["payload"], body["prev_hash"], digest
            )

    @property
    def path(self) -> Path:
        return self._path


def verify_audit_log(path: Path) -> tuple[bool, int, str]:
    """Return (ok, records_checked, error_message)."""
    prev = GENESIS_HASH
    expected_seq = 1
    count = 0
    if not path.exists():
        return True, 0, ""
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            digest = record.pop("hash", None)
            if record.get("prev_hash") != prev:
                return False, count, f"line {lineno}: broken chain (prev_hash mismatch)"
            if record.get("seq") != expected_seq:
                return False, count, f"line {lineno}: unexpected seq {record.get('seq')}"
            if _hash_record(record) != digest:
                return False, count, f"line {lineno}: hash mismatch (record modified)"
            prev = digest
            expected_seq += 1
            count += 1
    return True, count, ""
