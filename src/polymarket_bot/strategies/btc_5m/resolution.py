"""Resolution adapter for Polymarket "Bitcoin Up or Down - 5 Minutes" markets.

A market is tradable only if its payload matches a *registered and enabled*
rule version exactly: description text hash, resolution source, crypto market
configuration, outcomes, window length and fee schedule. The title is never
used to infer anything. See docs/research.md §5 for the evidence behind each
rule (verified on real resolved markets).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from polymarket_bot.domain.clock import datetime_to_ms
from polymarket_bot.domain.market import (
    ALLOWED_TICK_SIZES,
    FeeSchedule,
    MarketDefinition,
    OutcomeToken,
)

WINDOW_MS = 300_000
SERIES_SLUG = "btc-up-or-down-5m"
_SLUG_RE = re.compile(r"^btc-updown-5m-(\d{10})$")
_CONDITION_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_TOKEN_RE = re.compile(r"^\d{10,90}$")


@dataclass(frozen=True, slots=True)
class RuleVersion:
    rule_id: str
    description_sha256: str
    resolution_source: str
    crypto_market_config: Mapping[str, Any] | None
    effective_from_ms: int
    effective_to_ms: int | None
    settlement: str  # "spot" | "twap"
    twap_lookback_s: int | None
    outcomes: tuple[str, str] = ("Up", "Down")
    # Up wins iff final >= price_to_beat (ties resolve Up). VERIFIED on 11/11 markets.
    tie_goes_to: str = "Up"


# Verbatim texts are stored in tests/fixtures/gamma; hashes computed from them.
RULES: tuple[RuleVersion, ...] = (
    RuleVersion(
        rule_id="btc_5m_spot_v1",
        description_sha256="41fa2f3114b7263fcab42e3e0eadc41f70c525b28b8e14d12c0fe44603858eac",
        resolution_source="https://data.chain.link/streams/btc-usd",
        crypto_market_config=None,
        effective_from_ms=datetime_to_ms(datetime.fromisoformat("2026-02-12T00:00:00+00:00")),
        effective_to_ms=datetime_to_ms(datetime.fromisoformat("2026-08-07T00:00:00+00:00")),
        settlement="spot",
        twap_lookback_s=None,
    ),
    # btc_5m_twap30_v2 (2026-08-07 -> 2026-08-14) is NOT registered: its exact text
    # was not captured, so such markets are rejected (fail closed).
    RuleVersion(
        rule_id="btc_5m_twap60_v3",
        description_sha256="485ceb1dabc4aa12fb42c76184563b7378f01e5de9ded73df049c0d191cd5ad1",
        resolution_source="https://data.chain.link/streams/btc-usd-twap-60s-streams",
        crypto_market_config={
            "id": "btc-5m-twap-60",
            "asset": "btc",
            "duration": "5m",
            "twapEnabled": True,
            "twapLookbackSeconds": 60,
        },
        effective_from_ms=datetime_to_ms(datetime.fromisoformat("2026-08-14T00:00:00+00:00")),
        effective_to_ms=None,
        settlement="twap",
        twap_lookback_s=60,
    ),
)
RULES_BY_ID = {r.rule_id: r for r in RULES}


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    reasons: tuple[str, ...]
    rule: RuleVersion | None = None
    market: MarketDefinition | None = None
    price_to_beat: Decimal | None = None
    final_price: Decimal | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    winner: str | None
    consistent_with_rule: bool
    detail: str


def _json_list(value: Any, name: str, reasons: list[str]) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            reasons.append(f"{name} is not valid JSON")
            return []
        if isinstance(parsed, list):
            return parsed
    reasons.append(f"{name} missing or not a list")
    return []


def _iso_ms(value: Any, name: str, reasons: list[str]) -> int | None:
    if not isinstance(value, str) or not value:
        reasons.append(f"{name} missing")
        return None
    try:
        return datetime_to_ms(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        reasons.append(f"{name} not ISO-8601")
        return None


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def description_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iter_event_markets(payload: Any) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield (event, market) pairs from a Gamma ``/events`` or ``/markets`` payload."""
    if not isinstance(payload, list):
        return
    for item in payload:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("markets"), list):  # /events shape
            for market in item["markets"]:
                if isinstance(market, dict):
                    yield item, market
        elif isinstance(item.get("events"), list) and item["events"]:  # /markets shape
            event = item["events"][0] if isinstance(item["events"][0], dict) else {}
            yield event, item


def validate_market(
    event: Mapping[str, Any],
    market: Mapping[str, Any],
    *,
    enabled_rule_ids: Iterable[str],
    require_tradable: bool = True,
) -> ValidationResult:
    """Strict validation. ``require_tradable=False`` is used for settlement of past markets."""
    reasons: list[str] = []
    slug = str(market.get("slug") or "")
    m = _SLUG_RE.match(slug)
    if not m:
        reasons.append(f"slug {slug!r} is not a BTC 5m slug")
    series_slug = event.get("seriesSlug")
    if series_slug is None and isinstance(event.get("series"), list) and event["series"]:
        series_slug = event["series"][0].get("slug")
    if series_slug is not None and series_slug != SERIES_SLUG:
        reasons.append(f"series {series_slug!r} unexpected")

    description = market.get("description")
    if not isinstance(description, str) or not description:
        reasons.append("description missing")
        rule = None
    else:
        digest = description_hash(description)
        rule = next((r for r in RULES if r.description_sha256 == digest), None)
        if rule is None:
            reasons.append(f"resolution text not recognised (sha256 {digest[:12]})")
    if rule is not None:
        if market.get("resolutionSource") != rule.resolution_source:
            reasons.append("resolutionSource does not match rule")
        cfg = market.get("cryptoMarketConfig")
        if rule.crypto_market_config is not None and cfg != dict(rule.crypto_market_config):
            reasons.append(f"cryptoMarketConfig mismatch: {cfg}")
        if rule.crypto_market_config is None and cfg not in (None, {}):
            reasons.append("unexpected cryptoMarketConfig for spot rule")

    start_ms = _iso_ms(
        market.get("eventStartTime") or event.get("startTime"), "eventStartTime", reasons
    )
    end_ms = _iso_ms(market.get("endDate"), "endDate", reasons)
    if start_ms is not None and end_ms is not None and end_ms - start_ms != WINDOW_MS:
        reasons.append(f"window length {end_ms - start_ms}ms != 300000")
    if m and start_ms is not None and int(m.group(1)) * 1000 != start_ms:
        reasons.append("slug timestamp does not match eventStartTime")
    if rule is not None and start_ms is not None:
        if start_ms < rule.effective_from_ms or (
            rule.effective_to_ms is not None and start_ms >= rule.effective_to_ms
        ):
            reasons.append(f"start outside {rule.rule_id} effective period")

    outcomes = _json_list(market.get("outcomes"), "outcomes", reasons)
    token_ids = _json_list(market.get("clobTokenIds"), "clobTokenIds", reasons)
    if rule is not None and tuple(outcomes) != rule.outcomes:
        reasons.append(f"outcomes {outcomes} != {list(rule.outcomes)}")
    if (
        len(token_ids) != 2
        or len(set(token_ids)) != 2
        or not all(isinstance(t, str) and _TOKEN_RE.match(t) for t in token_ids)
    ):
        reasons.append("clobTokenIds must be two distinct numeric token ids")
    condition_id = str(market.get("conditionId") or "")
    if not _CONDITION_RE.match(condition_id):
        reasons.append("conditionId malformed")
    if market.get("negRisk") is not False:
        reasons.append("negRisk must be false")

    tick = _dec(market.get("orderPriceMinTickSize"))
    if tick is None or tick not in ALLOWED_TICK_SIZES:
        reasons.append(f"tick size {market.get('orderPriceMinTickSize')} unsupported")
    min_size = _dec(market.get("orderMinSize"))
    if min_size is None or min_size <= 0:
        reasons.append("orderMinSize missing")
    fee: FeeSchedule | None = None
    sched = market.get("feeSchedule")
    if market.get("feesEnabled") is not True or not isinstance(sched, dict):
        reasons.append("fee schedule unknown")
    else:
        rate, exponent = _dec(sched.get("rate")), _dec(sched.get("exponent"))
        if (
            rate is None
            or exponent is None
            or not (0 <= rate <= Decimal("0.1"))
            or not (0 <= exponent <= 2)
        ):
            reasons.append(f"fee schedule out of range: {sched}")
        else:
            fee = FeeSchedule(rate=rate, exponent=exponent, taker_only=bool(sched.get("takerOnly")))

    if require_tradable:
        if rule is not None and rule.rule_id not in set(enabled_rule_ids):
            reasons.append(f"rule {rule.rule_id} not enabled for trading")
        if market.get("enableOrderBook") is not True:
            reasons.append("order book disabled")
        if market.get("closed") is True:
            reasons.append("market closed")
        if market.get("active") is not True:
            reasons.append("market not active")

    meta = event.get("eventMetadata") if isinstance(event.get("eventMetadata"), dict) else {}
    ptb = _dec(meta.get("priceToBeat")) if meta else None
    final = _dec(meta.get("finalPrice")) if meta else None

    if reasons or rule is None or fee is None or tick is None or min_size is None:
        return ValidationResult(False, tuple(reasons), rule, None, ptb, final)
    assert start_ms is not None
    assert end_ms is not None
    definition = MarketDefinition(
        market_id=str(market.get("id") or ""),
        condition_id=condition_id,
        slug=slug,
        question=str(market.get("question") or ""),
        tokens=(OutcomeToken(token_ids[0], outcomes[0]), OutcomeToken(token_ids[1], outcomes[1])),
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        tick_size=tick,
        min_order_size=min_size,
        fee_schedule=fee,
        neg_risk=False,
        rule_id=rule.rule_id,
        accepting_orders=market.get("acceptingOrders") is True,
        description_sha256=rule.description_sha256,
    )
    return ValidationResult(True, (), rule, definition, ptb, final)


def resolved_outcome(
    event: Mapping[str, Any], market: Mapping[str, Any], rule: RuleVersion
) -> ResolutionOutcome:
    """Official outcome, only once Polymarket reports the market as resolved."""
    if market.get("closed") is not True:
        return ResolutionOutcome(None, True, "not closed")
    if market.get("umaResolutionStatus") != "resolved":
        return ResolutionOutcome(None, True, f"status {market.get('umaResolutionStatus')!r}")
    reasons: list[str] = []
    prices = _json_list(market.get("outcomePrices"), "outcomePrices", reasons)
    outcomes = _json_list(market.get("outcomes"), "outcomes", reasons)
    if reasons or sorted(map(str, prices)) != ["0", "1"] or len(outcomes) != 2:
        return ResolutionOutcome(None, True, f"ambiguous outcomePrices {prices}")
    winner = str(outcomes[[str(p) for p in prices].index("1")])
    meta = event.get("eventMetadata") if isinstance(event.get("eventMetadata"), dict) else {}
    ptb = _dec(meta.get("priceToBeat")) if meta else None
    final = _dec(meta.get("finalPrice")) if meta else None
    if ptb is not None and final is not None:
        expected = rule.outcomes[0] if final >= ptb else rule.outcomes[1]
        if expected != winner:
            return ResolutionOutcome(
                winner,
                False,
                f"rule predicts {expected} (final {final} vs ptb {ptb}) but paid {winner}",
            )
        return ResolutionOutcome(winner, True, "consistent with rule")
    return ResolutionOutcome(winner, True, "finalPrice not yet published")


def slug_for_window(start_ms: int) -> str:
    if start_ms % WINDOW_MS:
        raise ValueError("window start must be aligned to 5 minutes")
    return f"btc-updown-5m-{start_ms // 1000}"
