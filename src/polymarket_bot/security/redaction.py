"""Secret redaction for logs, exceptions, reports, prompts and tool outputs.

Two layers:
1. *Value* redaction: every secret value loaded by ``security/secrets.py`` is
   registered here and replaced verbatim wherever it appears.
2. *Pattern* redaction: generic shapes (EVM private keys, Anthropic keys,
   bearer tokens, auth headers, PEM blocks) are masked even if never registered.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import traceback
from collections.abc import Iterable
from types import TracebackType
from typing import Any

REDACTED = "[REDACTED]"

# A bare 64-hex pattern is intentionally NOT used: condition ids and transaction
# hashes have the same shape as private keys and must stay readable in the audit
# trail. Raw private keys are covered by value redaction (every loaded key is
# registered) and by the key-name pattern below.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(
        r"(?i)((?:poly_api_key|poly_passphrase|poly_signature|x-api-key|authorization|"
        r"api[_-]?secret|api[_-]?passphrase|private[_-]?key|anthropic_api_key)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)
_MIN_SECRET_LEN = 6


class SecretRegistry:
    """Thread-safe set of secret values to scrub."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()

    def register(self, value: str | None) -> None:
        if value and len(value) >= _MIN_SECRET_LEN:
            with self._lock:
                self._values.add(value)

    def values(self) -> tuple[str, ...]:
        with self._lock:
            # Longest first so overlapping secrets are fully removed.
            return tuple(sorted(self._values, key=len, reverse=True))

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


REGISTRY = SecretRegistry()


def redact(text: str, extra_values: Iterable[str] = ()) -> str:
    for value in (*REGISTRY.values(), *extra_values):
        if value:
            text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside JSON-like structures."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact_obj(v) for v in obj)
    return obj


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs the fully formatted message and exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed %-format args must not leak raw args
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info:
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(formatted)
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install_excepthook() -> None:
    """Make uncaught exceptions go through redaction before reaching stderr."""

    def _hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        sys.stderr.write(redact(text))

    sys.excepthook = _hook
