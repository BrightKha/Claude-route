"""Read recorded sessions back as RawMessages, in exact arrival order.

The replay engine (``app/replay_engine.py``) feeds these messages through the
*same* normalization code as live, advancing a :class:`SimulatedClock` to each
message's receive time. Out-of-order recordings are an error (fail closed).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from polymarket_bot.data.recorder import MARKET_DATA_SOURCES
from polymarket_bot.ports import RawMessage


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SessionInfo:
    path: Path
    synthetic: bool
    parts: tuple[Path, ...]


def open_session(path: Path) -> SessionInfo:
    """``path`` is a session directory or a single .jsonl file."""
    if path.is_file():
        meta_path = path.parent / "session.json"
        parts: tuple[Path, ...] = (path,)
    else:
        meta_path = path / "session.json"
        parts = tuple(sorted(path.glob("part-*.jsonl")))
    if not parts:
        raise ReplayError(f"no recording parts under {path}")
    synthetic = True  # unknown provenance is treated as synthetic
    if meta_path.exists():
        synthetic = bool(json.loads(meta_path.read_text(encoding="utf-8")).get("synthetic", True))
    return SessionInfo(path, synthetic, parts)


def iter_messages(session: SessionInfo, *, include_bot: bool = False) -> Iterator[RawMessage]:
    last_t = -1
    last_seq = 0
    for part in session.parts:
        with part.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    env = json.loads(line)
                    src, kind, t, seq = env["src"], env["kind"], int(env["t"]), int(env["seq"])
                except (ValueError, KeyError, TypeError) as exc:
                    raise ReplayError(f"{part.name}:{lineno}: corrupt envelope") from exc
                if seq <= last_seq:
                    raise ReplayError(f"{part.name}:{lineno}: sequence not increasing")
                last_seq = seq
                if src == "bot" and not include_bot:
                    continue
                if src != "bot":
                    # Only market data drives the simulated clock; bot records are informational.
                    if src not in MARKET_DATA_SOURCES:
                        raise ReplayError(f"{part.name}:{lineno}: unknown source {src!r}")
                    if t < last_t:
                        raise ReplayError(
                            f"{part.name}:{lineno}: time goes backwards ({t} < {last_t})"
                        )
                    last_t = t
                yield RawMessage(
                    source=src,
                    kind=kind,
                    payload=env.get("data"),
                    received_ms=t,
                    monotonic_ns=int(env.get("mono", 0)),
                    meta=env.get("meta"),
                )
