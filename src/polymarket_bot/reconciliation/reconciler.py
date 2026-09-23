"""Compare local state with what the exchange reports.

Any critical mismatch => STOP NEW ORDERS (HALTED, manual) until a later
reconciliation passes *and* the operator has looked (docs/incident-response.md).
An open order on the dedicated wallet that the bot never submitted is treated
as an anomaly (possible key compromise or manual interference).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polymarket_bot.config.app_config import ReconciliationConfig
from polymarket_bot.domain.orders import AccountSnapshot

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Mismatch:
    kind: str
    severity: str  # "critical" | "warning"
    token_id: str | None
    local: str
    remote: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    ok: bool
    ts_ms: int
    source: str
    mismatches: tuple[Mismatch, ...]
    checked_tokens: int

    @property
    def critical(self) -> tuple[Mismatch, ...]:
        return tuple(m for m in self.mismatches if m.severity == "critical")


@dataclass(frozen=True, slots=True)
class LocalState:
    positions: dict[str, Decimal]  # token_id -> shares (unsettled markets only)
    cash_usd: Decimal | None  # None when cash is not tracked for this venue
    open_order_ids: frozenset[str]  # exchange ids we believe are open
    known_order_ids: frozenset[str]  # every exchange id we ever received
    ignore_tokens: frozenset[str] = frozenset()  # e.g. settled markets awaiting redemption


class Reconciler:
    def __init__(self, config: ReconciliationConfig) -> None:
        self._cfg = config

    def compare(
        self, local: LocalState, remote: AccountSnapshot, now_ms: int
    ) -> ReconciliationReport:
        mismatches: list[Mismatch] = []
        if not remote.complete:
            mismatches.append(
                Mismatch("incomplete_remote", "critical", None, "-", "account snapshot incomplete")
            )
        tokens = (set(local.positions) | set(remote.positions)) - set(local.ignore_tokens)
        for token in sorted(tokens):
            ours = local.positions.get(token, ZERO)
            theirs = remote.positions.get(token, ZERO)
            if abs(ours - theirs) > self._cfg.share_tolerance:
                mismatches.append(Mismatch("position", "critical", token, str(ours), str(theirs)))
        if local.cash_usd is not None:
            diff = abs(local.cash_usd - remote.collateral_usd)
            if diff > self._cfg.balance_tolerance_usd:
                mismatches.append(
                    Mismatch(
                        "cash", "critical", None, str(local.cash_usd), str(remote.collateral_usd)
                    )
                )
        remote_ids = {o.exchange_order_id for o in remote.open_orders}
        mismatches.extend(
            Mismatch(
                "external_order",
                "critical",
                order.token_id,
                "not submitted by bot",
                f"{order.side} {order.original_size}@{order.price} id={order.exchange_order_id}",
            )
            for order in remote.open_orders
            if order.exchange_order_id not in local.known_order_ids
        )
        mismatches.extend(
            Mismatch("stale_local_open_order", "warning", None, order_id, "not open remotely")
            for order_id in sorted(local.open_order_ids - remote_ids)
        )
        ok = not any(m.severity == "critical" for m in mismatches)
        return ReconciliationReport(ok, now_ms, remote.source, tuple(mismatches), len(tokens))
