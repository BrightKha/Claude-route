"""Restricted MCP server (stdio) exposing :class:`BotReadModel`.

The tool list is closed and tested (tests/security/test_mcp_surface.py): read
tools plus two proposal tools. There is deliberately no tool to place a raw
order, cancel, withdraw, transfer, change risk configuration, reset the kill
switch or read any secret. MCP tool annotations are hints for the client only;
the enforcement is that such code paths do not exist in this process.

The server refuses to start when any secret variable is present in its
environment: it must run as a separate process without trading or API keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from polymarket_bot.config.app_config import McpConfig
from polymarket_bot.domain.clock import SystemClock
from polymarket_bot.mcp_server.read_model import BotReadModel
from polymarket_bot.security.secrets import secret_env_vars_present
from polymarket_bot.storage.sqlite_store import ProposalInbox, StateStore

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
PROPOSAL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)

INSTRUCTIONS = (
    "Read-only view of a Polymarket BTC 5m trading bot, plus proposal tools. "
    "Proposals are advisory: the bot's deterministic strategy and risk engine decide."
)


class McpStartupError(RuntimeError):
    pass


def build_server(model: BotReadModel) -> MCPServer:
    mcp = MCPServer("polymarket-bot", instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ_ONLY)
    def get_bot_status() -> dict[str, Any]:
        """Lifecycle state, kill switch and runtime heartbeat."""
        return model.get_bot_status()

    @mcp.tool(annotations=READ_ONLY)
    def get_account_summary() -> dict[str, Any]:
        """Cash, equity, exposure (paper or live ledger as published by the bot)."""
        return model.get_account_summary()

    @mcp.tool(annotations=READ_ONLY)
    def get_positions() -> dict[str, Any]:
        """Open positions as published by the bot."""
        return model.get_positions()

    @mcp.tool(annotations=READ_ONLY)
    def get_open_orders() -> dict[str, Any]:
        """Orders the bot considers open (including UNKNOWN)."""
        return model.get_open_orders()

    @mcp.tool(annotations=READ_ONLY)
    def get_market_snapshot() -> dict[str, Any]:
        """Latest market snapshot (books, reference price, time to expiry)."""
        return model.get_market_snapshot()

    @mcp.tool(annotations=READ_ONLY)
    def get_candidate_markets() -> dict[str, Any]:
        """Latest deterministic candidates with edge and rejection reasons."""
        return model.get_candidate_markets()

    @mcp.tool(annotations=READ_ONLY)
    def get_recent_trades(limit: int = 50) -> dict[str, Any]:
        """Most recent fills."""
        return model.get_recent_trades(limit)

    @mcp.tool(annotations=READ_ONLY)
    def get_pnl() -> dict[str, Any]:
        """Realized/unrealized PnL and fees."""
        return model.get_pnl()

    @mcp.tool(annotations=READ_ONLY)
    def get_risk_state(limit: int = 20) -> dict[str, Any]:
        """Kill switch, risk policy hash and recent risk decisions."""
        return model.get_risk_state(limit)

    @mcp.tool(annotations=READ_ONLY)
    def get_model_metrics() -> dict[str, Any]:
        """Calibration and performance metrics published by the bot."""
        return model.get_model_metrics()

    @mcp.tool(annotations=READ_ONLY)
    def get_system_health() -> dict[str, Any]:
        """Feed health, clock drift, watchdog and last reconciliation."""
        return model.get_system_health()

    @mcp.tool(annotations=READ_ONLY)
    def get_recent_events(limit: int = 20) -> dict[str, Any]:
        """Recent incidents and state transitions."""
        return model.get_recent_events(limit)

    @mcp.tool(annotations=READ_ONLY)
    def get_strategy_status() -> dict[str, Any]:
        """Strategy version, enabled flag and promotion stage."""
        return model.get_strategy_status()

    @mcp.tool(annotations=READ_ONLY)
    def get_proposal_status(proposal_id: str) -> dict[str, Any]:
        """Status of a proposal previously submitted with request_trade/request_close."""
        return model.get_proposal_status(proposal_id)

    @mcp.tool(annotations=PROPOSAL)
    def request_trade(
        market_slug: str, outcome: str, max_notional_usd: str, rationale: str
    ) -> dict[str, Any]:
        """Propose buying `outcome` in a BTC 5m market. Not an order: the bot decides."""
        return model.request_trade(market_slug, outcome, max_notional_usd, rationale)

    @mcp.tool(annotations=PROPOSAL)
    def request_close(market_slug: str, outcome: str, rationale: str) -> dict[str, Any]:
        """Propose closing a held position. Not an order: the bot decides."""
        return model.request_close(market_slug, outcome, rationale)

    return mcp


def open_model(data_dir: Path, config: McpConfig) -> BotReadModel:
    present = secret_env_vars_present()
    if present:
        raise McpStartupError(
            "refusing to start: secret variables present in the MCP environment "
            f"({', '.join(present)}); run the MCP server as a separate process without secrets"
        )
    if config.http_enabled:
        raise McpStartupError("HTTP transport is not implemented; use stdio")
    state_path = data_dir / "state.sqlite"
    if not state_path.exists():
        raise McpStartupError(f"no state database at {state_path}; start the bot first")
    store = StateStore(state_path, read_only=True)
    inbox = ProposalInbox(data_dir / "inbox.sqlite")
    return BotReadModel(store, inbox, config, SystemClock())


def run_stdio(data_dir: Path, config: McpConfig) -> None:
    build_server(open_model(data_dir, config)).run("stdio")
