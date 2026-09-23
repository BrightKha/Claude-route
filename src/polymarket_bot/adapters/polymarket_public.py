"""Public (unauthenticated) Polymarket REST: Gamma discovery + CLOB market data.

Implements ``MarketDiscoveryProvider`` and ``MarketDataProvider``. Endpoints and
payload shapes are documented in docs/research.md §2, §5 and backed by the real
fixtures in tests/fixtures.
"""

from __future__ import annotations

from polymarket_bot.adapters.http_base import PublicDataError, PublicHttp
from polymarket_bot.config.app_config import MarketDataConfig
from polymarket_bot.domain.clock import Clock
from polymarket_bot.ports import RawMessage


class GammaDiscovery:
    def __init__(
        self, config: MarketDataConfig, clock: Clock, http: PublicHttp | None = None
    ) -> None:
        self._http = http or PublicHttp(
            config.gamma_url,
            clock,
            source="gamma",
            timeout_s=config.rest_timeout_s,
            max_retries=config.rest_max_retries,
        )

    async def fetch_series_events(self, *, series_id: str, closed: bool, limit: int) -> RawMessage:
        params: dict[str, object] = {
            "series_id": series_id,
            "closed": str(closed).lower(),
            "limit": limit,
            "order": "startTime" if not closed else "endDate",
            "ascending": "true" if not closed else "false",
        }
        return await self._http.get_json("/events", params, kind="events")

    async def fetch_events_by_slugs(self, slugs: list[str]) -> RawMessage:
        if not slugs:
            raise ValueError("slugs must not be empty")
        return await self._http.get_json("/events", [("slug", s) for s in slugs], kind="events")

    async def fetch_market_by_slug(self, slug: str) -> RawMessage:
        return await self._http.get_json("/markets", {"slug": slug}, kind="markets")

    async def aclose(self) -> None:
        await self._http.aclose()


class ClobPublicData:
    def __init__(
        self, config: MarketDataConfig, clock: Clock, http: PublicHttp | None = None
    ) -> None:
        self._http = http or PublicHttp(
            config.clob_url,
            clock,
            source="clob_rest",
            timeout_s=config.rest_timeout_s,
            max_retries=config.rest_max_retries,
        )

    async def fetch_book(self, token_id: str) -> RawMessage:
        return await self._http.get_json("/book", {"token_id": token_id}, kind="book")

    async def fetch_clob_market(self, condition_id: str) -> RawMessage:
        return await self._http.get_json(f"/clob-markets/{condition_id}", None, kind="clob_market")

    async def fetch_server_time_ms(self) -> int:
        """``GET /time`` returns integer epoch *seconds* as plain text (VERIFIED)."""
        text = (await self._http.get_text("/time")).strip()
        if not text.isdigit():
            raise PublicDataError("unexpected /time payload")
        return int(text) * 1000

    async def aclose(self) -> None:
        await self._http.aclose()
