"""What the MCP server can see and do — implemented without the MCP SDK.

* Reads: the main state database opened **read-only** (``mode=ro``) plus the
  runtime status documents the bot publishes. No exchange, no network, no keys.
* Writes: only *proposals* into the separate inbox database, schema-validated
  and rate-limited. A proposal is not an order: the running bot re-derives a
  deterministic candidate, applies the Risk Engine and the execution policy,
  and may ignore it (docs/security.md "MCP").
* Every output is passed through secret redaction (defence in depth).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from polymarket_bot.config.app_config import McpConfig
from polymarket_bot.domain.clock import Clock
from polymarket_bot.risk.hard_caps import HARD_CAPS
from polymarket_bot.security.redaction import redact_obj
from polymarket_bot.storage.sqlite_store import ProposalInbox, StateStore

MINUTE_MS: Final = 60_000
SLUG_PATTERN: Final = r"^btc-updown-5m-\d{10}$"
MAX_LIST: Final = 200

READ_TOOLS: Final = (
    "get_bot_status",
    "get_account_summary",
    "get_positions",
    "get_open_orders",
    "get_market_snapshot",
    "get_candidate_markets",
    "get_recent_trades",
    "get_pnl",
    "get_risk_state",
    "get_model_metrics",
    "get_system_health",
    "get_recent_events",
    "get_strategy_status",
    "get_proposal_status",
)
PROPOSAL_TOOLS: Final = ("request_trade", "request_close")


class TradeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    market_slug: str = Field(pattern=SLUG_PATTERN)
    outcome: Literal["Up", "Down"]
    max_notional_usd: Decimal = Field(gt=0, le=HARD_CAPS["max_order_size_usd"].value)
    rationale: str = Field(min_length=1, max_length=1000)


class CloseProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    market_slug: str = Field(pattern=SLUG_PATTERN)
    outcome: Literal["Up", "Down"]
    rationale: str = Field(min_length=1, max_length=1000)


class ProposalRejectedError(ValueError):
    pass


class BotReadModel:
    def __init__(
        self, store: StateStore, inbox: ProposalInbox, config: McpConfig, clock: Clock
    ) -> None:
        if not store.read_only:
            raise ValueError("the MCP read model requires a read-only state store")
        self._store = store
        self._inbox = inbox
        self._cfg = config
        self._clock = clock

    @staticmethod
    def _out(obj: Any) -> dict[str, Any]:
        result = redact_obj(obj)
        return result if isinstance(result, dict) else {"result": result}

    def _status(self, name: str) -> dict[str, Any]:
        doc = self._store.read_status(name)
        if doc is None:
            return self._out({"available": False, "reason": f"runtime has not published {name}"})
        return self._out({"available": True, **doc})

    # ------------------------------------------------------------------ reads
    def get_bot_status(self) -> dict[str, Any]:
        state = self._store.load_bot_state()
        engaged, reason = self._store.kill_switch_state()
        return self._out(
            {
                "state": None if state is None else state[0],
                "state_reason": None if state is None else state[1],
                "manual_reset_required": None if state is None else state[2],
                "kill_switch_engaged": engaged,
                "kill_switch_reason": reason,
                "runtime": self._store.read_status("runtime"),
            }
        )

    def get_account_summary(self) -> dict[str, Any]:
        return self._out(
            {"latest_equity_mark": self._store.latest_equity_mark(), **self._status("portfolio")}
        )

    def get_positions(self) -> dict[str, Any]:
        return self._status("portfolio")

    def get_open_orders(self) -> dict[str, Any]:
        orders = [
            {
                "intent_id": r.intent.intent_id,
                "market_slug": r.intent.market_slug,
                "outcome": r.intent.outcome,
                "side": r.intent.side.value,
                "order_type": r.intent.order_type.value,
                "limit_price": str(r.intent.limit_price),
                "buy_amount_usd": None
                if r.intent.buy_amount_usd is None
                else str(r.intent.buy_amount_usd),
                "sell_shares": None if r.intent.sell_shares is None else str(r.intent.sell_shares),
                "status": r.status.value,
                "filled_shares": str(r.filled_shares),
                "updated_ms": r.updated_ms,
            }
            for r in self._store.load_orders(only_open=True)
        ]
        return self._out({"open_orders": orders})

    def get_market_snapshot(self) -> dict[str, Any]:
        return self._status("market")

    def get_candidate_markets(self) -> dict[str, Any]:
        return self._status("candidates")

    def get_recent_trades(self, limit: int = 50) -> dict[str, Any]:
        return self._out({"fills": self._store.recent_fills(min(max(limit, 1), MAX_LIST))})

    def get_pnl(self) -> dict[str, Any]:
        return self._out(
            {"latest_equity_mark": self._store.latest_equity_mark(), **self._status("pnl")}
        )

    def get_risk_state(self, limit: int = 20) -> dict[str, Any]:
        engaged, reason = self._store.kill_switch_state()
        return self._out(
            {
                "kill_switch_engaged": engaged,
                "kill_switch_reason": reason,
                "risk_policy_hash": self._store.get_meta("risk_policy_hash"),
                "recent_decisions": self._store.recent_risk_decisions(min(max(limit, 1), MAX_LIST)),
            }
        )

    def get_model_metrics(self) -> dict[str, Any]:
        return self._status("model_metrics")

    def get_system_health(self) -> dict[str, Any]:
        return self._out(
            {**self._status("health"), "last_reconciliation": self._store.last_reconciliation()}
        )

    def get_recent_events(self, limit: int = 20) -> dict[str, Any]:
        n = min(max(limit, 1), MAX_LIST)
        return self._out(
            {
                "incidents": self._store.recent_incidents(n),
                "state_transitions": self._store.state_transitions(n),
            }
        )

    def get_strategy_status(self) -> dict[str, Any]:
        return self._status("strategy")

    def get_proposal_status(self, proposal_id: str) -> dict[str, Any]:
        return self._out({"proposal": self._inbox.get(proposal_id)})

    # ------------------------------------------------------------------ proposals
    def _submit(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = self._clock.now_ms()
        if self._inbox.count_since(now - MINUTE_MS) >= self._cfg.max_proposals_per_minute:
            raise ProposalRejectedError("proposal rate limit reached")
        proposal_id = f"pr-{uuid.uuid4().hex[:16]}"
        self._inbox.submit(
            proposal_id=proposal_id, created_ms=now, source="mcp", kind=kind, payload=payload
        )
        return self._out(
            {
                "proposal_id": proposal_id,
                "status": "PENDING",
                "expires_in_s": self._cfg.proposal_ttl_s,
                "note": "A proposal is not an order. The bot re-derives the trade "
                "deterministically and the Risk Engine decides; it may be ignored.",
            }
        )

    def request_trade(
        self, market_slug: str, outcome: str, max_notional_usd: str, rationale: str
    ) -> dict[str, Any]:
        proposal = TradeProposal.model_validate(
            {
                "market_slug": market_slug,
                "outcome": outcome,
                "max_notional_usd": max_notional_usd,
                "rationale": rationale,
            }
        )
        return self._submit("trade", proposal.model_dump(mode="json"))

    def request_close(self, market_slug: str, outcome: str, rationale: str) -> dict[str, Any]:
        proposal = CloseProposal.model_validate(
            {"market_slug": market_slug, "outcome": outcome, "rationale": rationale}
        )
        return self._submit("close", proposal.model_dump(mode="json"))
