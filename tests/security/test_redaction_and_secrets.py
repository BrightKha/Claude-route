"""No secret may leak through logs, exceptions, repr, config or audit records."""

from __future__ import annotations

import io
import json
import logging
import pickle

import pytest

from polymarket_bot.audit.audit_log import AuditLog
from polymarket_bot.config.loader import ConfigError, load_config
from polymarket_bot.config.settings import EnvSettings
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.monitoring.logging_setup import JsonFormatter
from polymarket_bot.security.redaction import REDACTED, REGISTRY, RedactingFilter, redact
from polymarket_bot.security.secrets import (
    MissingSecretError,
    Secret,
    load_anthropic_api_key,
    load_polymarket_credentials,
    secret_env_vars_present,
)

FAKE_KEY = "0x" + "ab12" * 16  # 64 hex chars, obviously fake
# Built dynamically so the repository secret scanner never sees a literal secret shape.
ANT_PREFIX = "sk-" + "ant-"
PEM_BEGIN = "-----BEGIN EC " + "PRIVATE KEY-----"
PEM_END = "-----END EC " + "PRIVATE KEY-----"
FAKE_WALLET = "0x" + "cd" * 20
CONDITION_ID = "0xc77927db1e825c26dfadd89a4113dd0c4cc2609a2a3f9cb455546662ba074676"


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


def _capture_logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(f"test-redaction-{id(stream)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_loaded_private_key_never_appears_in_logs_or_exceptions(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", FAKE_KEY)
    monkeypatch.setenv("POLYMARKET_WALLET_ADDRESS", FAKE_WALLET)
    creds = load_polymarket_credentials()
    logger, stream = _capture_logger()
    logger.info("key is %s", creds.private_key.reveal())
    try:
        raise RuntimeError(f"boom with {creds.private_key.reveal()}")
    except RuntimeError:
        logger.exception("failure")
    out = stream.getvalue()
    assert FAKE_KEY not in out
    assert FAKE_KEY[2:] not in out
    assert REDACTED in out


def test_secret_wrapper_hides_value_everywhere():
    s = Secret("super-secret-value-123")
    assert "super-secret" not in repr(s)
    assert "super-secret" not in str(s)
    assert "super-secret" not in f"{s}"
    assert "super-secret" not in json.dumps({"s": repr(s)})
    with pytest.raises(TypeError):
        pickle.dumps(s)
    assert s.reveal() == "super-secret-value-123"


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abcdefghijklmnop1234",
        "POLY_API_KEY=abcd-efgh-ijkl-mnop",
        '{"api_secret": "c2VjcmV0LXNlY3JldA=="}',
        "anthropic key " + ANT_PREFIX + "api03-AAAAAAAAAAAAAAAAAAAAAA",
        "private_key: deadbeefdeadbeefdeadbeef",
        PEM_BEGIN + "\nMHcCAQEE\n" + PEM_END,
    ],
)
def test_pattern_redaction(text):
    out = redact(text)
    assert REDACTED in out
    for fragment in (
        "abcdefghijklmnop1234",
        "abcd-efgh",
        "c2VjcmV0",
        "AAAAAAAAAAAAAAAA",
        "deadbeef",
        "MHcCAQEE",
    ):
        assert fragment not in out


def test_condition_ids_and_addresses_stay_readable():
    text = f"market {CONDITION_ID} wallet {FAKE_WALLET}"
    assert redact(text) == text


def test_invalid_key_error_does_not_echo_value(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "not-a-key-but-secret-ish")
    with pytest.raises(MissingSecretError) as exc:
        load_polymarket_credentials()
    assert "not-a-key" not in str(exc.value)


def test_wallet_address_required_to_prevent_implicit_deployment(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", FAKE_KEY)
    monkeypatch.delenv("POLYMARKET_WALLET_ADDRESS", raising=False)
    with pytest.raises(MissingSecretError, match="WALLET_ADDRESS"):
        load_polymarket_credentials()


def test_partial_api_credentials_rejected(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", FAKE_KEY)
    monkeypatch.setenv("POLYMARKET_WALLET_ADDRESS", FAKE_WALLET)
    monkeypatch.setenv("POLYMARKET_API_KEY", "only-the-key")
    with pytest.raises(MissingSecretError):
        load_polymarket_credentials()


def test_secret_env_names_listed_without_values(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANT_PREFIX + "test-XXXXXXXXXXXXXXXXXXXX")
    names = secret_env_vars_present()
    assert names == ["ANTHROPIC_API_KEY"] or "ANTHROPIC_API_KEY" in names
    key = load_anthropic_api_key()
    assert key is not None and ANT_PREFIX not in repr(key)


def test_env_settings_have_no_secret_fields():
    forbidden = ("key", "secret", "passphrase", "mnemonic", "seed", "token", "password")
    for name in EnvSettings.model_fields:
        assert not any(f in name.lower() for f in forbidden), name


def test_env_settings_do_not_read_dotenv_files():
    assert EnvSettings.model_config.get("env_file") is None


def test_yaml_with_secret_keys_is_rejected(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("mode: paper\nllm:\n  api_key: placeholder\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(cfg)


def test_audit_payloads_are_redacted(tmp_path):
    Secret("an-api-secret-value-xyz")
    log = AuditLog(tmp_path / "audit.jsonl", SimulatedClock(1), fsync=False)
    log.append("test", {"note": "leaked an-api-secret-value-xyz here"})
    content = (tmp_path / "audit.jsonl").read_text()
    assert "an-api-secret-value-xyz" not in content
    assert REDACTED in content
