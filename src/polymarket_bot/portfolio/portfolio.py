"""Position accounting and PnL.

* Cost basis includes entry fees; exit fees reduce proceeds.
* Open positions are marked at the *best bid* (liquidation value) — never the
  mid — so equity, drawdown and daily loss are conservative.
* Settlement pays 1 per winning share, 0 per losing share.
* Any accounting invariant violation raises :class:`InvariantError`; the
  runtime treats it as critical (kill switch).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from polymarket_bot.domain.orders import Fill
from polymarket_bot.domain.types import Side
from polymarket_bot.risk.engine import PortfolioView, PositionView

ZERO = Decimal(0)
ONE = Decimal(1)
# Pro-rata cost removal is quantized so every running sum stays exact within the
# 28-digit Decimal context (found by the cash-conservation property test).
ACCOUNTING_Q = Decimal("1e-10")


class InvariantError(RuntimeError):
    pass


@dataclass
class Position:
    token_id: str
    condition_id: str
    outcome: str
    market_slug: str
    window_end_ms: int
    shares: Decimal = ZERO
    cost_basis_usd: Decimal = ZERO
    realized_pnl_usd: Decimal = ZERO
    fees_usd: Decimal = ZERO
    opened_ms: int = 0
    entry_shares_total: Decimal = ZERO
    entry_cost_total: Decimal = ZERO

    @property
    def avg_cost_per_share(self) -> Decimal:
        return self.cost_basis_usd / self.shares if self.shares > 0 else ZERO


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    token_id: str
    condition_id: str
    outcome: str
    market_slug: str
    opened_ms: int
    closed_ms: int
    shares: Decimal
    cost_usd: Decimal
    proceeds_usd: Decimal
    fees_usd: Decimal
    pnl_usd: Decimal
    exit_kind: str  # "sell" | "settlement"


@dataclass
class Portfolio:
    cash_usd: Decimal
    start_of_day_equity_usd: Decimal = ZERO
    peak_equity_usd: Decimal = ZERO
    equity_usd: Decimal = ZERO
    positions: dict[str, Position] = field(default_factory=dict)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    realized_pnl_usd: Decimal = ZERO
    fees_paid_usd: Decimal = ZERO
    consecutive_losses: int = 0
    last_loss_ms: int | None = None
    current_day: str = ""
    marks: dict[str, Decimal] = field(default_factory=dict)  # token -> last bid used

    @classmethod
    def with_cash(cls, cash: Decimal, now_ms: int) -> Portfolio:
        p = cls(cash_usd=cash, start_of_day_equity_usd=cash, peak_equity_usd=cash, equity_usd=cash)
        p.current_day = _day(now_ms)
        return p

    # ------------------------------------------------------------------ fills
    def apply_fill(self, fill: Fill, *, market_slug: str, window_end_ms: int) -> ClosedTrade | None:
        pos = self.positions.get(fill.token_id)
        if fill.side is Side.BUY:
            cost = fill.price * fill.shares + fill.fee_usd
            if cost > self.cash_usd + Decimal("0.000001"):
                raise InvariantError(f"fill {fill.fill_id} costs {cost} > cash {self.cash_usd}")
            if pos is None:
                pos = Position(
                    fill.token_id,
                    fill.condition_id,
                    fill.outcome,
                    market_slug,
                    window_end_ms,
                    opened_ms=fill.ts_ms,
                )
                self.positions[fill.token_id] = pos
            self.cash_usd -= cost
            pos.shares += fill.shares
            pos.cost_basis_usd += cost
            pos.fees_usd += fill.fee_usd
            pos.entry_shares_total += fill.shares
            pos.entry_cost_total += cost
            self.fees_paid_usd += fill.fee_usd
            return None
        if pos is None or fill.shares > pos.shares:
            held = pos.shares if pos else ZERO
            raise InvariantError(f"sell {fill.shares} of {fill.token_id} but hold {held}")
        proceeds = fill.price * fill.shares - fill.fee_usd
        if fill.shares == pos.shares:
            cost_removed = pos.cost_basis_usd
        else:
            cost_removed = (pos.cost_basis_usd * fill.shares / pos.shares).quantize(ACCOUNTING_Q)
        pnl = proceeds - cost_removed
        self.cash_usd += proceeds
        pos.shares -= fill.shares
        pos.cost_basis_usd -= cost_removed
        pos.realized_pnl_usd += pnl
        pos.fees_usd += fill.fee_usd
        self.realized_pnl_usd += pnl
        self.fees_paid_usd += fill.fee_usd
        if pos.shares == 0:
            return self._close(pos, fill.ts_ms, "sell")
        return None

    def settle(self, condition_id: str, winning_outcome: str, ts_ms: int) -> list[ClosedTrade]:
        closed: list[ClosedTrade] = []
        for pos in list(self.positions.values()):
            if pos.condition_id != condition_id or pos.shares == 0:
                continue
            payout = pos.shares if pos.outcome == winning_outcome else ZERO
            pnl = payout - pos.cost_basis_usd
            self.cash_usd += payout
            pos.realized_pnl_usd += pnl
            self.realized_pnl_usd += pnl
            pos.cost_basis_usd = ZERO
            pos.shares = ZERO
            closed.append(self._close(pos, ts_ms, "settlement"))
        return closed

    def _close(self, pos: Position, ts_ms: int, kind: str) -> ClosedTrade:
        trade = ClosedTrade(
            token_id=pos.token_id,
            condition_id=pos.condition_id,
            outcome=pos.outcome,
            market_slug=pos.market_slug,
            opened_ms=pos.opened_ms,
            closed_ms=ts_ms,
            shares=pos.entry_shares_total,
            cost_usd=pos.entry_cost_total,
            proceeds_usd=pos.entry_cost_total + pos.realized_pnl_usd,
            fees_usd=pos.fees_usd,
            pnl_usd=pos.realized_pnl_usd,
            exit_kind=kind,
        )
        self.closed_trades.append(trade)
        del self.positions[pos.token_id]
        self.marks.pop(pos.token_id, None)
        if trade.pnl_usd < 0:
            self.consecutive_losses += 1
            self.last_loss_ms = ts_ms
        else:
            self.consecutive_losses = 0
        return trade

    # ------------------------------------------------------------------ marking
    def mark(self, bids: dict[str, Decimal | None], now_ms: int) -> None:
        """Mark positions at best bid; missing bids keep the last mark (flagged by caller)."""
        for token, bid in bids.items():
            if token in self.positions and bid is not None:
                self.marks[token] = bid
        value = sum(
            (pos.shares * self.marks.get(tok, ZERO) for tok, pos in self.positions.items()), ZERO
        )
        self.equity_usd = self.cash_usd + value
        day = _day(now_ms)
        if day != self.current_day:
            self.current_day = day
            self.start_of_day_equity_usd = self.equity_usd
        self.peak_equity_usd = max(self.peak_equity_usd, self.equity_usd)

    @property
    def exposure_usd(self) -> Decimal:
        return sum((p.cost_basis_usd for p in self.positions.values()), ZERO)

    def view(
        self, *, pending_buy_usd: Decimal, pending_sell_shares: dict[str, Decimal]
    ) -> PortfolioView:
        return PortfolioView(
            cash_usd=self.cash_usd,
            equity_usd=self.equity_usd,
            start_of_day_equity_usd=self.start_of_day_equity_usd,
            peak_equity_usd=self.peak_equity_usd,
            positions={
                t: PositionView(t, p.condition_id, p.shares, p.cost_basis_usd)
                for t, p in self.positions.items()
            },
            pending_buy_usd=pending_buy_usd,
            pending_sell_shares=pending_sell_shares,
            consecutive_losses=self.consecutive_losses,
            last_loss_ms=self.last_loss_ms,
        )


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")
