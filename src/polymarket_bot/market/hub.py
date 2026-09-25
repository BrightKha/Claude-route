"""MarketDataHub: the single owner of market state for the decision pipeline.

It consumes RawMessages (live or replayed — same code path), maintains the
registry of validated markets, order books, reference prices and clock drift,
and builds :class:`MarketSnapshot` objects listing every staleness reason.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from polymarket_bot.config.app_config import AppConfig
from polymarket_bot.data.clock_drift import ClockDriftEstimator
from polymarket_bot.data.reference_prices import ReferencePriceState
from polymarket_bot.domain.clock import Clock
from polymarket_bot.domain.market import MarketDefinition
from polymarket_bot.domain.snapshot import MarketSnapshot, ReferenceView, TokenQuote
from polymarket_bot.market.clob_messages import (
    ApplyStats,
    MessageError,
    apply_market_event,
    apply_rest_book,
    split_frame,
)
from polymarket_bot.market.orderbook import OrderBookState
from polymarket_bot.ports import RawMessage
from polymarket_bot.strategies.btc_5m.resolution import (
    RULES_BY_ID,
    iter_event_markets,
    resolved_outcome,
    validate_market,
)
from polymarket_bot.watchdog.health import HealthRegistry

log = logging.getLogger(__name__)

PTB_CHECKS_KEPT = 50
MAX_MESSAGE_KEYS = 32


@dataclass
class TrackedMarket:
    definition: MarketDefinition
    official_price_to_beat: Decimal | None = None
    official_final_price: Decimal | None = None
    winner: str | None = None
    resolution_consistent: bool = True
    last_refresh_ms: int = 0


@dataclass
class HubStats:
    frames: int = 0
    malformed_frames: int = 0
    rejected_markets: int = 0
    rejection_reasons: dict[str, int] | None = None


class MarketDataHub:
    def __init__(self, config: AppConfig, clock: Clock, health: HealthRegistry) -> None:
        self._cfg = config
        self._clock = clock
        self._health = health
        self.markets: dict[str, TrackedMarket] = {}
        self.books: dict[str, OrderBookState] = {}
        md = config.market_data
        self.reference = ReferencePriceState(
            symbol=md.reference_symbol,
            secondary_symbol=md.secondary_symbol,
            vol_halflife_s=config.fair_value.vol_halflife_s,
        )
        self.drift = ClockDriftEstimator()
        self.market_ws_connected = False
        self.rtds_connected = False
        self.stats = HubStats(rejection_reasons={})
        self.apply_stats = ApplyStats()
        self.resolution_anomalies: list[str] = []
        # Observability only (docs/diagnostics.md): never read by a decision.
        self.message_counts: dict[str, int] = {}
        self.ptb_checks: deque[dict[str, Any]] = deque(maxlen=PTB_CHECKS_KEPT)

    # ------------------------------------------------------------------ ingestion
    def on_raw(self, msg: RawMessage) -> list[str]:
        """Apply one message. Returns token ids newly requiring a subscription."""
        src, kind = msg.source, msg.kind
        key = f"{src}:{kind}"
        if key in self.message_counts or len(self.message_counts) < MAX_MESSAGE_KEYS:
            self.message_counts[key] = self.message_counts.get(key, 0) + 1
        if src == "clob_ws":
            self._on_clob_ws(msg)
        elif src == "rtds":
            self._on_rtds(msg)
        elif src == "gamma" and kind in ("events", "markets"):
            return self._on_gamma(msg.payload, msg.received_ms)
        elif src == "clob_rest" and kind == "book":
            self._on_rest_book(msg)
        return []

    def _on_clob_ws(self, msg: RawMessage) -> None:
        if msg.kind == "connection":
            connected = isinstance(msg.payload, dict) and msg.payload.get("state") == "connected"
            self.market_ws_connected = connected
            if not connected:
                for book in self.books.values():
                    book.invalidate("market websocket disconnected")
                self.drift.reset()
            self._health.market_stream(connected=connected)
            return
        self._health.market_stream(connected=self.market_ws_connected, msg_ms=msg.received_ms)
        if msg.kind != "ws_frame" or not isinstance(msg.payload, str):
            return
        self.stats.frames += 1
        try:
            events = split_frame(msg.payload)
        except MessageError:
            self.stats.malformed_frames += 1
            return
        for event in events:
            for exchange_ms in apply_market_event(
                event, self.books, msg.received_ms, self.apply_stats
            ):
                self.drift.add(exchange_ms, msg.received_ms)
        self._health.clock_drift(self.drift.estimate_ms())

    def _on_rtds(self, msg: RawMessage) -> None:
        if msg.kind == "connection":
            connected = isinstance(msg.payload, dict) and msg.payload.get("state") == "connected"
            self.rtds_connected = connected
            self._health.reference_stream(connected=connected)
            return
        if msg.kind == "ws_frame" and isinstance(msg.payload, str):
            if self.reference.on_frame(msg.payload, msg.received_ms):
                self._health.reference_stream(connected=self.rtds_connected, msg_ms=msg.received_ms)

    def _on_rest_book(self, msg: RawMessage) -> None:
        payload: Any = msg.payload
        token = str(payload.get("asset_id")) if isinstance(payload, dict) else ""
        book = self.books.get(token)
        if book is None:
            return
        try:
            apply_rest_book(payload, book, msg.received_ms)
        except MessageError as exc:
            book.invalidate(f"REST book rejected: {exc}")

    def _on_gamma(self, payload: Any, received_ms: int) -> list[str]:
        new_tokens: list[str] = []
        enabled = self._cfg.strategy.enabled_rule_ids
        for event, market in iter_event_markets(payload):
            cid = str(market.get("conditionId") or "")
            tracked = self.markets.get(cid)
            if tracked is None:
                result = validate_market(event, market, enabled_rule_ids=enabled)
                if not result.ok or result.market is None:
                    if market.get("closed") is not True:
                        self.stats.rejected_markets += 1
                        for reason in result.reasons:
                            assert self.stats.rejection_reasons is not None
                            key = reason.split(":")[0][:60]
                            self.stats.rejection_reasons[key] = (
                                self.stats.rejection_reasons.get(key, 0) + 1
                            )
                    continue
                tracked = TrackedMarket(result.market)
                self.markets[cid] = tracked
                for token in result.market.token_ids:
                    if token not in self.books:
                        self.books[token] = OrderBookState(token)
                        new_tokens.append(token)
            # refresh mutable facts (price to beat, acceptance, resolution)
            settle = validate_market(
                event, market, enabled_rule_ids=enabled, require_tradable=False
            )
            tracked.last_refresh_ms = received_ms
            if settle.price_to_beat is not None:
                if tracked.official_price_to_beat is None:
                    self._record_ptb_check(tracked.definition, settle.price_to_beat, received_ms)
                tracked.official_price_to_beat = settle.price_to_beat
            if settle.final_price is not None:
                tracked.official_final_price = settle.final_price
            accepting = market.get("acceptingOrders") is True and market.get("closed") is not True
            if accepting != tracked.definition.accepting_orders:
                tracked.definition = replace(tracked.definition, accepting_orders=accepting)
            rule = RULES_BY_ID.get(tracked.definition.rule_id)
            if rule is not None and tracked.winner is None:
                outcome = resolved_outcome(event, market, rule)
                if outcome.winner is not None:
                    tracked.winner = outcome.winner
                    tracked.resolution_consistent = outcome.consistent_with_rule
                    if not outcome.consistent_with_rule:
                        self.resolution_anomalies.append(
                            f"{tracked.definition.slug}: {outcome.detail}"
                        )
        return new_tokens

    def _record_ptb_check(self, d: MarketDefinition, official: Decimal, received_ms: int) -> None:
        """Evidence: when Gamma publishes the price to beat, and does RTDS agree?"""
        stream = self.reference.twap60.exact(d.window_start_ms)
        diff = abs(float(official / stream.value) - 1) * 1e4 if stream is not None else None
        check = {
            "slug": d.slug,
            "official": float(official),
            "stream_twap_exact": float(stream.value) if stream is not None else None,
            "diff_bps": None if diff is None else round(diff, 4),
            "first_seen_after_start_s": round((received_ms - d.window_start_ms) / 1000, 1),
            "first_seen_after_end_s": round((received_ms - d.window_end_ms) / 1000, 1),
        }
        self.ptb_checks.append(check)
        log.debug("official price to beat first seen: %s", check)

    # ------------------------------------------------------------------ views
    def tracked_tokens(self, now_ms: int, *, grace_ms: int = 60_000) -> list[str]:
        horizon = now_ms + self._cfg.market_data.discovery_lookahead_s * 1000
        tokens: list[str] = []
        for tm in self.markets.values():
            d = tm.definition
            if d.window_end_ms + grace_ms >= now_ms and d.window_start_ms <= horizon:
                tokens.extend(d.token_ids)
        return tokens

    def active_markets(self, now_ms: int) -> list[TrackedMarket]:
        """Markets whose window is currently running."""
        return [
            tm
            for tm in self.markets.values()
            if tm.definition.window_start_ms <= now_ms < tm.definition.window_end_ms
        ]

    def snapshot(self, condition_id: str) -> MarketSnapshot | None:
        tracked = self.markets.get(condition_id)
        if tracked is None:
            return None
        now = self._clock.now_ms()
        risk = self._cfg.risk
        d = tracked.definition
        reasons: list[str] = []
        quotes: list[TokenQuote] = []
        ticks: list[Decimal] = []
        for tok in d.tokens:
            book_state = self.books.get(tok.token_id)
            book = book_state.snapshot() if book_state else None
            if book is None:
                why = book_state.invalid_reason if book_state else "untracked"
                reasons.append(f"{tok.outcome} book invalid: {why}")
            elif now - book.received_ms > risk.max_data_age_ms:
                reasons.append(f"{tok.outcome} book stale {now - book.received_ms}ms")
            if book is not None and book.tick_size is not None:
                ticks.append(book.tick_size)
            window = self._cfg.market_data.depth_window
            quotes.append(
                TokenQuote(
                    token_id=tok.token_id,
                    outcome=tok.outcome,
                    best_bid=book.best_bid if book else None,
                    best_ask=book.best_ask if book else None,
                    mid=book.mid if book else None,
                    spread=book.spread if book else None,
                    bid_depth_usd=book.depth_usd("bid", window) if book else Decimal(0),
                    ask_depth_usd=book.depth_usd("ask", window) if book else Decimal(0),
                    last_trade_price=book.last_trade_price if book else None,
                    imbalance=book.imbalance() if book else None,
                    book_received_ms=book.received_ms if book else None,
                    book_exchange_ms=book.exchange_ms if book else None,
                    book_age_ms=(now - book.received_ms) if book else None,
                    book_valid=book is not None,
                    book=book,
                )
            )
        if ticks:
            d = replace(d, tick_size=max(ticks))
        ref = self.reference
        spot, twap = ref.spot.latest(), ref.twap60.latest()
        spot_age = now - spot.received_ms if spot else None
        twap_age = now - twap.received_ms if twap else None
        if spot is None or twap is None:
            reasons.append("reference price missing")
        elif max(spot_age or 0, twap_age or 0) > risk.max_reference_age_ms:
            reasons.append(f"reference stale spot={spot_age}ms twap={twap_age}ms")
        if ref.is_suspect(now):
            reasons.append("reference feed suspect (outlier hold)")
        dispersion = ref.dispersion_bps()
        if dispersion is not None and dispersion > self._cfg.market_data.max_source_dispersion_bps:
            reasons.append(f"source dispersion {dispersion:.1f}bps")
        if not self.market_ws_connected:
            reasons.append("market websocket disconnected")
        if not self.rtds_connected:
            reasons.append("reference websocket disconnected")
        if self.drift.estimate_ms() is None:
            reasons.append("clock drift unknown")
        if not tracked.resolution_consistent:
            reasons.append("resolution anomaly on this market")
        ptb = ref.price_to_beat(
            d.window_start_ms,
            tracked.official_price_to_beat,
            tolerance_bps=self._cfg.fair_value.price_to_beat_tolerance_bps,
        )
        if now >= d.window_start_ms and not ptb.verified:
            reasons.append(f"price to beat not verified: {ptb.detail}")
        secondary = ref.secondary.latest()
        reference = ReferenceView(
            spot=spot.value if spot else None,
            spot_observed_ms=spot.observed_ms if spot else None,
            spot_age_ms=spot_age,
            twap=twap.value if twap else None,
            twap_observed_ms=twap.observed_ms if twap else None,
            twap_age_ms=twap_age,
            price_to_beat=ptb.value,
            price_to_beat_source=ptb.source,
            price_to_beat_verified=ptb.verified,
            secondary_spot=secondary.value if secondary else None,
            secondary_age_ms=(now - secondary.received_ms) if secondary else None,
            dispersion_bps=dispersion,
        )
        return MarketSnapshot(
            snapshot_id=f"snap-{uuid.uuid4().hex[:16]}",
            monotonic_ns=self._clock.monotonic_ns(),
            utc_ms=now,
            market=d,
            quotes=(quotes[0], quotes[1]),
            reference=reference,
            time_to_expiry_ms=d.window_end_ms - now,
            time_since_start_ms=now - d.window_start_ms,
            feeds_connected=self.market_ws_connected and self.rtds_connected,
            stale_reasons=tuple(reasons),
        )
