"""Periodic discovery of BTC 5m markets through Gamma.

Slugs for the current and upcoming windows are derived from the clock, but a
market is only tracked after the resolution adapter validated its full payload
(``MarketDataHub._on_gamma``). Ended-but-unresolved markets are re-queried
until their official outcome is known (for settlement).
"""

from __future__ import annotations

from polymarket_bot.config.app_config import MarketDataConfig
from polymarket_bot.market.hub import MarketDataHub
from polymarket_bot.ports import MarketDiscoveryProvider, RawMessage
from polymarket_bot.strategies.btc_5m.resolution import WINDOW_MS, slug_for_window

MAX_SLUGS_PER_REQUEST = 20
UNRESOLVED_LOOKBACK_MS = 30 * 60_000


def discovery_slugs(now_ms: int, hub: MarketDataHub, config: MarketDataConfig) -> list[str]:
    current = now_ms - now_ms % WINDOW_MS
    horizon = now_ms + config.discovery_lookahead_s * 1000
    slugs: list[str] = []
    start = current
    while start <= horizon:
        slugs.append(slug_for_window(start))
        start += WINDOW_MS
    for tracked in hub.markets.values():
        d = tracked.definition
        if tracked.winner is None and now_ms - UNRESOLVED_LOOKBACK_MS <= d.window_end_ms <= now_ms:
            slugs.append(d.slug)
    return list(dict.fromkeys(slugs))[:MAX_SLUGS_PER_REQUEST]


async def refresh_markets(
    provider: MarketDiscoveryProvider, hub: MarketDataHub, config: MarketDataConfig, now_ms: int
) -> tuple[RawMessage, list[str]]:
    """Fetch, apply and return (raw message for recording, new token ids to subscribe)."""
    msg = await provider.fetch_events_by_slugs(discovery_slugs(now_ms, hub, config))
    return msg, hub.on_raw(msg)
