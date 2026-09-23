"""Bounded, fail-closed HTTP GET for *public, idempotent* endpoints only.

Order submission never goes through this helper (it lives in the live adapter
and is never retried blindly).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

import httpx

from polymarket_bot.domain.clock import Clock
from polymarket_bot.ports import RawMessage

log = logging.getLogger(__name__)


class PublicDataError(RuntimeError):
    """Raised when public data cannot be fetched or parsed. Callers must NO-TRADE."""


class PublicHttp:
    def __init__(
        self,
        base_url: str,
        clock: Clock,
        *,
        source: str,
        timeout_s: float,
        max_retries: int,
        client: httpx.AsyncClient | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._clock = clock
        self._source = source
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={"User-Agent": "polymarket-bot/0.1 (conservative research client)"},
            follow_redirects=False,
        )
        self._rng = rng or random.Random()  # noqa: S311 - jitter only, not security

    async def get_json(
        self, path: str, params: dict[str, Any] | list[tuple[str, Any]] | None, kind: str
    ) -> RawMessage:
        text = await self._get(path, params)
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise PublicDataError(f"{self._source}{path}: invalid JSON") from exc
        return RawMessage(
            source=self._source,
            kind=kind,
            payload=payload,
            received_ms=self._clock.now_ms(),
            monotonic_ns=self._clock.monotonic_ns(),
            meta={
                "path": path,
                "params": params if isinstance(params, dict) else dict(params or []),
            },
        )

    async def get_text(self, path: str) -> str:
        return await self._get(path, None)

    async def _get(self, path: str, params: dict[str, Any] | list[tuple[str, Any]] | None) -> str:
        url = f"{self._base}{path}"
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = _retry_after_s(resp)
                    raise httpx.HTTPStatusError(
                        f"retryable status {resp.status_code} (retry-after {retry_after})",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                return resp.text
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                status = (
                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                )
                if status is not None and 400 <= status < 500 and status != 429:
                    break  # client errors are not retried
                if attempt < self._max_retries:
                    delay = min(8.0, 0.5 * 2**attempt) * (1 + self._rng.random() * 0.25)
                    if status == 429 and isinstance(exc, httpx.HTTPStatusError):
                        delay = max(delay, _retry_after_s(exc.response))
                    await asyncio.sleep(delay)
        raise PublicDataError(f"{self._source}{path}: {type(last_exc).__name__}") from last_exc

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after_s(resp: httpx.Response) -> float:
    try:
        return min(30.0, float(resp.headers.get("retry-after", "1")))
    except ValueError:
        return 1.0
