"""Settlement keeps the bot's portfolio and the venue ledger identical.

Regression: ``_settle`` passed a token id to ``MarketDefinition.other`` (which
takes an outcome label) and handed the venue an ``OutcomeToken`` object, so the
losing position stayed in the paper venue's ledger. Found by the one-day
SYNTHETIC validation run (reconciliation mismatch -> HALTED).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from polymarket_bot.app.build import assemble
from polymarket_bot.config.loader import load_config
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.domain.orders import Fill
from polymarket_bot.domain.types import Side, TradingMode
from polymarket_bot.market.hub import TrackedMarket
from tests.factories import COND, DOWN, T0, UP, make_market

ROOT = Path(__file__).resolve().parents[2]
D = Decimal


def _fill(token: str, outcome: str, n: int) -> Fill:
    return Fill(
        fill_id=f"f{n}",
        intent_id=None,
        exchange_order_id=None,
        condition_id=COND,
        token_id=token,
        outcome=outcome,
        side=Side.BUY,
        price=D("0.40"),
        shares=D("10"),
        fee_usd=D("0.17"),
        ts_ms=T0,
        liquidity="taker",
        source="test",
    )


@pytest.mark.parametrize("winner", ["Up", "Down"])
def test_settlement_updates_both_ledgers_identically(tmp_path: Path, winner: str) -> None:
    config = load_config(ROOT / "configs" / "paper.yaml")
    clock = SimulatedClock(T0 + 400_000)
    asm = assemble(config, mode=TradingMode.PAPER, clock=clock, data_dir=tmp_path)
    core, paper = asm.core, asm.paper
    assert paper is not None
    market = make_market()
    asm.hub.markets[COND] = TrackedMarket(market, winner=winner)
    for n, (token, outcome) in enumerate(((UP, "Up"), (DOWN, "Down"))):
        fill = _fill(token, outcome, n)
        core.portfolio.apply_fill(fill, market_slug=market.slug, window_end_ms=T0 + 300_000)
        paper.positions[token] = fill.shares
        paper.cash_usd -= fill.price * fill.shares + fill.fee_usd
    assert paper.cash_usd == core.portfolio.cash_usd

    core._settle(clock.now_ms())

    assert core.portfolio.positions == {}
    assert paper.positions == {}
    assert paper.cash_usd == core.portfolio.cash_usd
    assert asm.store.settlements() == {COND: winner}
