"""Structured logging: human-readable console + machine-readable JSON lines.

Every handler carries the redaction filter, so secrets are scrubbed regardless
of which logger emits them (including third-party SDK loggers).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from polymarket_bot.security.redaction import RedactingFilter, install_excepthook, redact

_STD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(
            {
                key: value
                for key, value in vars(record).items()
                if key not in _STD_ATTRS and not key.startswith("_")
            }
        )
        if record.exc_text:
            payload["exc"] = record.exc_text
        return redact(json.dumps(payload, default=str, sort_keys=True))


def setup_logging(level: str = "INFO", json_path: Path | None = None) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)
    redactor = RedactingFilter()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    console.addFilter(redactor)
    root.addHandler(console)

    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(json_path, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)

    # Never let HTTP client libraries log request headers at DEBUG.
    for noisy in ("httpx", "httpcore", "httpx2", "websockets", "anthropic", "polymarket"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, logging.getLogger().level))
    install_excepthook()
