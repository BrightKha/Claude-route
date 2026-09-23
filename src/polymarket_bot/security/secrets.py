"""The ONLY place that reads secret material from the environment.

Callers (enforced by tests/security):
- ``load_polymarket_credentials`` -> ``adapters/polymarket_live.py`` only.
- ``load_anthropic_api_key``      -> ``llm/claude_client.py`` only.

Secret containers never reveal their value through ``repr``/``str``/format,
and every loaded value is registered for log redaction.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Final

from polymarket_bot.security.redaction import REGISTRY

_HEX_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")

POLYMARKET_SECRET_ENV_VARS: Final = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
)
ANTHROPIC_SECRET_ENV_VARS: Final = ("ANTHROPIC_API_KEY",)
ALL_SECRET_ENV_VARS: Final = POLYMARKET_SECRET_ENV_VARS + ANTHROPIC_SECRET_ENV_VARS
LIVE_REQUIRED_SECRET: Final = POLYMARKET_SECRET_ENV_VARS[0]


class MissingSecretError(RuntimeError):
    pass


class Secret:
    """Opaque secret wrapper. ``reveal()`` is the only way to get the value."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        self.__value = value
        REGISTRY.register(value)

    def reveal(self) -> str:
        return self.__value

    def __repr__(self) -> str:
        return "Secret([REDACTED])"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "[REDACTED]"

    def __reduce__(self) -> tuple[object, ...]:  # block pickling
        raise TypeError("Secret objects cannot be serialized")

    def __bool__(self) -> bool:
        return bool(self.__value)


@dataclass(frozen=True, slots=True)
class PolymarketCredentials:
    private_key: Secret
    wallet_address: str  # public, not secret
    api_key: Secret | None
    api_secret: Secret | None
    api_passphrase: Secret | None


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def load_polymarket_credentials() -> PolymarketCredentials:
    key = _env("POLYMARKET_PRIVATE_KEY")
    if key is None:
        raise MissingSecretError("POLYMARKET_PRIVATE_KEY is not set")
    if not _HEX_KEY.match(key):
        # Do not echo the value, not even partially.
        raise MissingSecretError("POLYMARKET_PRIVATE_KEY has an invalid format")
    wallet = _env("POLYMARKET_WALLET_ADDRESS")
    if wallet is None or not _ADDRESS.match(wallet):
        raise MissingSecretError(
            "POLYMARKET_WALLET_ADDRESS must be an explicit, existing wallet address "
            "(prevents implicit wallet deployment, docs/research.md §1.1)"
        )
    parts = [_env(n) for n in POLYMARKET_SECRET_ENV_VARS[1:]]
    if any(parts) and not all(parts):
        raise MissingSecretError(
            "API key, secret and passphrase must be set together or not at all"
        )
    api_key, api_secret, api_pass = (Secret(p) if p else None for p in parts)
    return PolymarketCredentials(Secret(key), wallet, api_key, api_secret, api_pass)


def load_anthropic_api_key() -> Secret | None:
    value = _env("ANTHROPIC_API_KEY")
    return Secret(value) if value else None


def secret_env_vars_present() -> list[str]:
    """Names (never values) of secret variables present in this process."""
    return [name for name in ALL_SECRET_ENV_VARS if _env(name)]
