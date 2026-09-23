"""Load and hash YAML configuration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from polymarket_bot.config.app_config import AppConfig

# Keys that must never appear in a committed YAML file.
_FORBIDDEN_KEY_FRAGMENTS = (
    "private_key",
    "secret",
    "passphrase",
    "api_key",
    "mnemonic",
    "seed_phrase",
)


class ConfigError(ValueError):
    pass


def _check_no_secrets(node: Any, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in _FORBIDDEN_KEY_FRAGMENTS):
                raise ConfigError(
                    f"secret-like key {path + str(key)!r} is not allowed in YAML; use env vars"
                )
            _check_no_secrets(value, f"{path}{key}.")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _check_no_secrets(item, f"{path}{i}.")


def load_config(path: str | Path) -> AppConfig:
    raw_text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text) or {}
    if not isinstance(data, dict):
        raise ConfigError("top-level YAML must be a mapping")
    _check_no_secrets(data)
    risk_file = data.pop("risk_policy_file", None)
    if risk_file is not None:
        risk_path = (Path(path).parent / str(risk_file)).resolve()
        risk_data = yaml.safe_load(risk_path.read_text(encoding="utf-8")) or {}
        _check_no_secrets(risk_data)
        if "risk" in data:
            raise ConfigError("use either inline 'risk' or 'risk_policy_file', not both")
        data["risk"] = risk_data
    return AppConfig.model_validate(data)


def config_hash(config: AppConfig) -> str:
    canonical = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
