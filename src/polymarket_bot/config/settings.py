"""Process environment settings. Contains NO secrets by design.

Secret material (private key, API credentials, Anthropic key) is read
exclusively by ``security/secrets.py`` loaders that are called only from the
two adapters that need them. A security test asserts this model never grows a
secret-looking field.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,  # never read .env implicitly; the operator injects env vars
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    trading_mode: Literal["disabled", "paper", "replay", "live"] = "disabled"
    live_trading_enabled: bool = False
    live_confirmation: str = Field("", repr=False)
    operator_jurisdiction: str = ""
    data_source: Literal["polymarket", "replay", "synthetic"] = "polymarket"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    bot_data_dir: str | None = None


def load_env_settings() -> EnvSettings:
    return EnvSettings()
