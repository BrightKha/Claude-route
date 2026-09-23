"""Strict parsing of CLOB market-channel frames and REST books.

Wire shapes: SDK ``models/clob/market_events.py`` + docs (docs/research.md §4).
A frame is JSON (object or array of objects) with ``event_type``. Anything that
cannot be parsed is *counted and reported*; the book it concerns (if any) is
invalidated by the caller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from polymarket_bot.market.orderbook import OrderBookState

KNOWN_EVENTS = frozenset(
    {
        "book",
        "price_change",
        "last_trade_price",
        "tick_size_change",
        "best_bid_ask",
        "new_market",
        "market_resolved",
    }
)


class MessageError(ValueError):
    pass


def dec(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise MessageError(f"{field}: missing/invalid")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise MessageError(f"{field}: not a number {value!r}") from exc
    if not result.is_finite():
        raise MessageError(f"{field}: not finite")
    return result


def opt_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except ValueError as exc:
        raise MessageError(f"timestamp not an integer: {value!r}") from exc


def parse_levels(raw: Any, field: str) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(raw, list):
        raise MessageError(f"{field} must be a list")
    out: list[tuple[Decimal, Decimal]] = []
    for i, lvl in enumerate(raw):
        if not isinstance(lvl, dict):
            raise MessageError(f"{field}[{i}] must be an object")
        out.append(
            (
                dec(lvl.get("price"), f"{field}[{i}].price"),
                dec(lvl.get("size"), f"{field}[{i}].size"),
            )
        )
    return out


def split_frame(text: str) -> list[dict[str, Any]]:
    """Decode a WS text frame into event dicts. Raises MessageError."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise MessageError("frame is not JSON") from exc
    items = data if isinstance(data, list) else [data]
    events: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise MessageError("event is not an object")
        events.append(item)
    return events


@dataclass
class ApplyStats:
    applied: int = 0
    ignored_unknown_event: int = 0
    malformed: int = 0
    untracked: int = 0
    resolved_markets: list[tuple[str, str | None]] | None = None


def apply_market_event(
    event: dict[str, Any],
    books: dict[str, OrderBookState],
    received_ms: int,
    stats: ApplyStats,
) -> list[int]:
    """Apply one event to tracked books. Returns exchange timestamps seen (for drift)."""
    etype = event.get("event_type") or event.get("type")
    if etype not in KNOWN_EVENTS:
        stats.ignored_unknown_event += 1
        return []
    ts_seen: list[int] = []
    try:
        exchange_ms = opt_ms(event.get("timestamp"))
        if exchange_ms is not None:
            ts_seen.append(exchange_ms)
        if etype == "book":
            token = str(event.get("asset_id") or "")
            book = books.get(token)
            if book is None:
                stats.untracked += 1
                return ts_seen
            tick = event.get("tick_size")
            book.apply_snapshot(
                parse_levels(event.get("bids"), "bids"),
                parse_levels(event.get("asks"), "asks"),
                received_ms=received_ms,
                exchange_ms=exchange_ms,
                book_hash=str(event["hash"]) if event.get("hash") else None,
                tick_size=dec(tick, "tick_size") if tick is not None else None,
            )
            if event.get("last_trade_price") not in (None, ""):
                book.last_trade_price = dec(event["last_trade_price"], "last_trade_price")
            stats.applied += 1
        elif etype == "price_change":
            changes = event.get("price_changes")
            if not isinstance(changes, list):
                raise MessageError("price_changes must be a list")
            # The echoed best bid/ask is checked after *all* changes of the event
            # have been applied (the last echo per token wins).
            echoes: dict[str, tuple[Any, Any]] = {}
            for ch in changes:
                if not isinstance(ch, dict):
                    raise MessageError("price_change entry must be an object")
                token = str(ch.get("asset_id") or "")
                book = books.get(token)
                if book is None:
                    stats.untracked += 1
                    continue
                side = str(ch.get("side", "")).upper()
                try:
                    book.apply_level(
                        side,
                        dec(ch.get("price"), "price"),
                        dec(ch.get("size"), "size"),
                        received_ms=received_ms,
                        exchange_ms=exchange_ms,
                    )
                except MessageError as exc:
                    book.invalidate(f"malformed price_change: {exc}")
                    stats.malformed += 1
                    continue
                echoes[token] = (ch.get("best_bid"), ch.get("best_ask"))
                stats.applied += 1
            for token, (bb, ba) in echoes.items():
                try:
                    books[token].verify_top(
                        dec(bb, "best_bid") if bb not in (None, "") else None,
                        dec(ba, "best_ask") if ba not in (None, "") else None,
                    )
                except MessageError as exc:
                    books[token].invalidate(f"malformed best bid/ask echo: {exc}")
                    stats.malformed += 1
        elif etype == "tick_size_change":
            book = books.get(str(event.get("asset_id") or ""))
            if book is None:
                stats.untracked += 1
                return ts_seen
            book.set_tick_size(dec(event.get("new_tick_size"), "new_tick_size"))
            stats.applied += 1
        elif etype == "last_trade_price":
            book = books.get(str(event.get("asset_id") or ""))
            if book is None:
                stats.untracked += 1
                return ts_seen
            book.last_trade_price = dec(event.get("price"), "price")
            stats.applied += 1
        elif etype == "market_resolved":
            if stats.resolved_markets is None:
                stats.resolved_markets = []
            winner = event.get("winning_asset_id") or event.get("winning_token_id")
            stats.resolved_markets.append(
                (str(event.get("market") or ""), str(winner) if winner else None)
            )
            stats.applied += 1
        else:  # best_bid_ask / new_market: informational only
            stats.applied += 1
    except MessageError:
        stats.malformed += 1
        token = str(event.get("asset_id") or "")
        if token in books:
            books[token].invalidate("malformed event")
    return ts_seen


def apply_rest_book(payload: Any, book: OrderBookState, received_ms: int) -> None:
    """Apply a REST ``/book`` payload (bids ascending, asks descending — never trusted)."""
    if not isinstance(payload, dict):
        raise MessageError("book payload must be an object")
    if str(payload.get("asset_id")) != book.token_id:
        raise MessageError("book payload is for another token")
    tick = payload.get("tick_size")
    book.apply_snapshot(
        parse_levels(payload.get("bids"), "bids"),
        parse_levels(payload.get("asks"), "asks"),
        received_ms=received_ms,
        exchange_ms=opt_ms(payload.get("timestamp")),
        book_hash=str(payload["hash"]) if payload.get("hash") else None,
        tick_size=dec(tick, "tick_size") if tick is not None else None,
    )
    if payload.get("last_trade_price") not in (None, ""):
        book.last_trade_price = dec(payload["last_trade_price"], "last_trade_price")


def build_market_subscribe(token_ids: list[str]) -> str:
    return json.dumps(
        {"type": "market", "assets_ids": sorted(token_ids), "custom_feature_enabled": True},
        separators=(",", ":"),
    )


def build_market_update(token_ids: list[str], *, subscribe: bool) -> str:
    frame: dict[str, Any] = {
        "operation": "subscribe" if subscribe else "unsubscribe",
        "assets_ids": sorted(token_ids),
    }
    if subscribe:
        frame["custom_feature_enabled"] = True
    return json.dumps(frame, separators=(",", ":"))
