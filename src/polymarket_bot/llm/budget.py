"""LLM cost and rate control (MAX_LLM_CALLS_PER_MINUTE/HOUR, MAX_LLM_SPEND_PER_DAY).

A call is allowed only if, *before* it is made, the worst-case cost of that
call (every input byte a token, every allowed output token spent) still fits in
the remaining daily budget. The actual cost is then booked from the usage the
API reports; when the outcome is unknown (timeout, transport error) the
worst-case reservation is booked instead. Daily spend is seeded from the state
store so a restart cannot reset the budget.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from polymarket_bot.config.app_config import LLMConfig

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
PER_MTOK = Decimal(1_000_000)


def day_start_ms(now_ms: int) -> int:
    d = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


def usage_cost_usd(cfg: LLMConfig, usage: TokenUsage) -> Decimal:
    inp = cfg.input_price_per_mtok_usd / PER_MTOK
    out = cfg.output_price_per_mtok_usd / PER_MTOK
    return (
        usage.input_tokens * inp
        + usage.cache_creation_input_tokens * inp * cfg.cache_write_price_multiplier
        + usage.cache_read_input_tokens * inp * cfg.cache_read_price_multiplier
        + usage.output_tokens * out
    )


def worst_case_cost_usd(cfg: LLMConfig, prompt_bytes: int) -> Decimal:
    """Upper bound: a token is never shorter than one byte; output capped by max_tokens.

    With server-side fallback a declined attempt may be partly billed before the
    fallback model runs, so the bound is doubled.
    """
    attempts = 2 if cfg.server_side_fallback else 1
    return attempts * usage_cost_usd(cfg, TokenUsage(prompt_bytes, cfg.max_tokens))


class LLMBudget:
    def __init__(self, cfg: LLMConfig, *, spent_today_usd: Decimal, now_ms: int) -> None:
        self._cfg = cfg
        self._calls: deque[int] = deque()
        self._day = day_start_ms(now_ms)
        self.spent_today_usd = spent_today_usd
        self._last_call_by_market: dict[str, int] = {}

    def _roll(self, now_ms: int) -> None:
        day = day_start_ms(now_ms)
        if day != self._day:
            self._day = day
            self.spent_today_usd = Decimal(0)
        while self._calls and now_ms - self._calls[0] >= HOUR_MS:
            self._calls.popleft()

    def blocked_reason(self, now_ms: int, market_key: str, prompt_bytes: int) -> str | None:
        """None when a call may be made now; otherwise the (loggable) reason."""
        self._roll(now_ms)
        cfg = self._cfg
        last = self._last_call_by_market.get(market_key)
        if last is not None and now_ms - last < cfg.debounce_per_market_s * 1000:
            return "debounced: market reviewed recently"
        per_minute = sum(1 for t in self._calls if now_ms - t < MINUTE_MS)
        if per_minute >= cfg.max_calls_per_minute:
            return f"rate: {per_minute} calls in the last minute"
        if len(self._calls) >= cfg.max_calls_per_hour:
            return f"rate: {len(self._calls)} calls in the last hour"
        worst = worst_case_cost_usd(cfg, prompt_bytes)
        if self.spent_today_usd + worst > cfg.max_spend_per_day_usd:
            return (
                f"budget: spent {self.spent_today_usd:.4f} + worst case {worst:.4f} "
                f"> {cfg.max_spend_per_day_usd}"
            )
        return None

    def record_call(self, now_ms: int, market_key: str, cost_usd: Decimal) -> None:
        self._roll(now_ms)
        self._calls.append(now_ms)
        self._last_call_by_market[market_key] = now_ms
        self.spent_today_usd += cost_usd
