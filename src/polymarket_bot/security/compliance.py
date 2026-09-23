"""Jurisdiction / geoblock gate for LIVE trading.

Polymarket restricts trading in many countries (docs/research.md §6), and using
a VPN to bypass restrictions violates its Terms of Service. This module only
*checks*; it contains no circumvention logic and must never gain any.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from polymarket_bot.config.app_config import ComplianceConfig

log = logging.getLogger(__name__)


def attestation_check(config: ComplianceConfig, jurisdiction: str) -> tuple[bool, str]:
    code = jurisdiction.strip().upper()
    if not code:
        return False, "OPERATOR_JURISDICTION not set"
    if len(code) != 2 or not code.isalpha():
        return False, "OPERATOR_JURISDICTION must be an ISO 3166-1 alpha-2 code"
    if code in config.blocked_countries:
        return False, f"{code} is on Polymarket's blocked list; live trading refused"
    if code in config.close_only_countries:
        return False, f"{code} is close-only; opening positions is not permitted"
    return True, f"attested {code}"


def interpret_geoblock_payload(payload: Any) -> tuple[bool, str]:
    """Fail closed on anything but an explicit ``blocked: false``."""
    if not isinstance(payload, dict):
        return False, "geoblock response not an object"
    blocked = payload.get("blocked")
    if blocked is False:
        return True, f"geoblock endpoint: not blocked (country={payload.get('country')})"
    if blocked is True:
        return False, f"geoblock endpoint: BLOCKED (country={payload.get('country')})"
    return False, "geoblock response missing 'blocked' boolean"


async def geoblock_check(config: ComplianceConfig, timeout_s: float = 5.0) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(config.geoblock_url)
            resp.raise_for_status()
            return interpret_geoblock_payload(resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        return False, f"geoblock endpoint unavailable: {type(exc).__name__}"


async def compliance_gate(config: ComplianceConfig, jurisdiction: str) -> tuple[bool, str]:
    ok, detail = attestation_check(config, jurisdiction)
    if not ok:
        return ok, detail
    if config.require_geoblock_endpoint_for_live:
        geo_ok, geo_detail = await geoblock_check(config)
        return geo_ok, f"{detail}; {geo_detail}"
    return ok, detail
