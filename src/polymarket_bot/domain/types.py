"""Core enums shared by every layer. Pure, no I/O."""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """Order types supported by Polymarket CLOB V2 (docs/research.md §2)."""

    GTC = "GTC"
    GTD = "GTD"
    FOK = "FOK"
    FAK = "FAK"


class TradingMode(StrEnum):
    DISABLED = "disabled"
    PAPER = "paper"
    REPLAY = "replay"
    LIVE = "live"


class BotState(StrEnum):
    DISABLED = "DISABLED"
    INITIALIZING = "INITIALIZING"
    SYNCING = "SYNCING"
    PAPER = "PAPER"
    LIVE = "LIVE"
    HALTED = "HALTED"
    KILL_SWITCH = "KILL_SWITCH"
    DEAD = "DEAD"


class OrderStatus(StrEnum):
    INTENT = "INTENT"  # recorded locally, not yet sent
    SUBMITTING = "SUBMITTING"  # request in flight
    LIVE = "LIVE"  # resting on the book
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"  # includes FAK remainder cancellation
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"  # outcome of a submission could not be established

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }

    @property
    def is_open(self) -> bool:
        return self in {
            OrderStatus.INTENT,
            OrderStatus.SUBMITTING,
            OrderStatus.LIVE,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.UNKNOWN,
        }


class OrderPurpose(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class CandidateAction(StrEnum):
    TRADE = "TRADE"
    NO_TRADE = "NO_TRADE"
