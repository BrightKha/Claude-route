"""The MCP surface is closed, read-only (except proposals) and secret-free."""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from polymarket_bot.config.app_config import McpConfig
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.mcp_server.read_model import (
    PROPOSAL_TOOLS,
    READ_TOOLS,
    BotReadModel,
    ProposalRejectedError,
)
from polymarket_bot.mcp_server.server import McpStartupError, build_server, open_model
from polymarket_bot.security.secrets import Secret
from polymarket_bot.storage.sqlite_store import ProposalInbox, StateStore
from tests.factories import T0

ROOT = Path(__file__).resolve().parents[2]
MCP_PKG = ROOT / "src" / "polymarket_bot" / "mcp_server"
SLUG = "btc-updown-5m-1790127600"
FORBIDDEN_WORDS = (
    "execute", "raw", "order_", "withdraw", "transfer", "cancel", "kill", "disable",
    "secret", "key", "wallet", "config", "limit", "approve", "sign", "deposit",
)  # fmt: skip


def _model(tmp: Path, **cfg: Any) -> tuple[BotReadModel, StateStore, ProposalInbox]:
    writer = StateStore(tmp / "state.sqlite")
    writer.publish_status("portfolio", ts_ms=T0, body={"cash_usd": "200", "positions": []})
    writer.close()
    store = StateStore(tmp / "state.sqlite", read_only=True)
    inbox = ProposalInbox(tmp / "inbox.sqlite")
    model = BotReadModel(store, inbox, McpConfig.model_validate(cfg), SimulatedClock(T0))
    return model, store, inbox


async def test_tool_list_is_exactly_the_documented_surface(tmp_path: Path) -> None:
    model, _, _ = _model(tmp_path)
    tools = await build_server(model).list_tools()
    names = {t.name for t in tools}
    assert names == set(READ_TOOLS) | set(PROPOSAL_TOOLS)
    for t in tools:
        if t.name in READ_TOOLS:
            assert t.annotations is not None and t.annotations.read_only_hint is True
        else:
            assert t.annotations is not None and t.annotations.destructive_hint is False


def test_no_dangerous_tool_names() -> None:
    for name in (*READ_TOOLS, *PROPOSAL_TOOLS):
        assert not any(w in name for w in FORBIDDEN_WORDS), name


def test_mcp_package_cannot_reach_execution_or_secrets() -> None:
    banned_prefixes = (
        "polymarket_bot.execution",
        "polymarket_bot.adapters",
        "polymarket_bot.llm",
        "polymarket_bot.lifecycle",
        "polymarket_bot.app",
        "polymarket_bot.promotion",
        "anthropic",
        "polymarket",
        "httpx",
        "websockets",
    )
    for path in MCP_PKG.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
                if node.module == "polymarket_bot.security.secrets":
                    assert {a.name for a in node.names} == {"secret_env_vars_present"}
            for n in names:
                hit = [b for b in banned_prefixes if n == b or n.startswith(b + ".")]
                assert not hit, f"{path.name} imports {n}"


def test_refuses_to_start_with_secrets_in_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    StateStore(tmp_path / "state.sqlite").close()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x" * 20)
    with pytest.raises(McpStartupError, match="ANTHROPIC_API_KEY"):
        open_model(tmp_path, McpConfig())


def test_refuses_http_and_missing_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "POLYMARKET_PRIVATE_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(McpStartupError, match="no state database"):
        open_model(tmp_path, McpConfig())
    with pytest.raises(McpStartupError, match="HTTP"):
        open_model(tmp_path, McpConfig(http_enabled=True))


def test_state_store_is_read_only(tmp_path: Path) -> None:
    _, store, _ = _model(tmp_path)
    with pytest.raises(PermissionError):  # application layer
        store.publish_status("x", ts_ms=T0, body={})
    with pytest.raises(sqlite3.OperationalError, match="readonly"):  # SQLite layer (mode=ro)
        store._conn.execute("DELETE FROM runtime_status")
    writer = StateStore(tmp_path / "w.sqlite")
    with pytest.raises(ValueError, match="read-only"):
        BotReadModel(writer, ProposalInbox(tmp_path / "i.sqlite"), McpConfig(), SimulatedClock(T0))


def test_reads_work_and_are_redacted(tmp_path: Path) -> None:
    leaked = "mcp-leak-" + "q" * 24
    Secret(leaked)
    writer = StateStore(tmp_path / "state.sqlite")
    writer.publish_status("health", ts_ms=T0, body={"note": f"token {leaked}"})
    writer.close()
    model, _, _ = _model(tmp_path)
    assert model.get_positions()["data"]["cash_usd"] == "200"
    assert leaked not in json.dumps(model.get_system_health())
    assert model.get_market_snapshot()["available"] is False
    assert model.get_open_orders() == {"open_orders": []}
    assert model.get_bot_status()["kill_switch_engaged"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"market_slug": "eth-updown-5m-1790127600"},
        {"market_slug": SLUG + "; DROP TABLE"},
        {"outcome": "Yes"},
        {"max_notional_usd": "0"},
        {"max_notional_usd": "-5"},
        {"max_notional_usd": "100.01"},  # above the code-level hard cap
        {"rationale": ""},
    ],
)
def test_trade_proposal_validation(tmp_path: Path, kwargs: dict[str, str]) -> None:
    model, _, inbox = _model(tmp_path)
    args = {"market_slug": SLUG, "outcome": "Up", "max_notional_usd": "5", "rationale": "edge"}
    args.update(kwargs)
    with pytest.raises(ValidationError):
        model.request_trade(**args)
    assert inbox.pending() == []


def test_proposals_are_rate_limited_and_only_queued(tmp_path: Path) -> None:
    model, _, inbox = _model(tmp_path, max_proposals_per_minute=2)
    r = model.request_trade(SLUG, "Up", "5", "edge looks real")
    assert r["status"] == "PENDING"
    model.request_close(SLUG, "Up", "thesis invalidated")
    with pytest.raises(ProposalRejectedError):
        model.request_trade(SLUG, "Down", "5", "again")
    pending = inbox.pending()
    assert [p["kind"] for p in pending] == ["trade", "close"]
    assert model.get_proposal_status(r["proposal_id"])["proposal"]["status"] == "PENDING"


async def test_tools_callable_through_mcp(tmp_path: Path) -> None:
    model, _, inbox = _model(tmp_path)
    server = build_server(model)
    result = await server.call_tool("get_positions", {})
    assert not result.is_error
    result = await server.call_tool(
        "request_trade",
        {"market_slug": SLUG, "outcome": "Up", "max_notional_usd": "5", "rationale": "x"},
    )
    assert not result.is_error
    assert len(inbox.pending()) == 1
