"""Bot lifecycle state machine with strictly controlled transitions.

    DISABLED -> INITIALIZING -> SYNCING -> PAPER | LIVE
    PAPER/LIVE -> HALTED (data outage, mismatch, exception) -> SYNCING (auto only if recoverable)
    any -> KILL_SWITCH (loss limit, invariant breach, operator) -> DISABLED (manual reset only)
    any -> DEAD (unrecoverable; process must exit)

Entering LIVE requires a :class:`LiveAuthorization`, which only
``promotion.live_lock.evaluate_live_lock`` can mint.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from polymarket_bot.domain.clock import Clock
from polymarket_bot.domain.types import BotState

log = logging.getLogger(__name__)

S = BotState
ALLOWED_TRANSITIONS: dict[BotState, frozenset[BotState]] = {
    S.DISABLED: frozenset({S.INITIALIZING, S.KILL_SWITCH, S.DEAD}),
    S.INITIALIZING: frozenset({S.SYNCING, S.HALTED, S.DISABLED, S.KILL_SWITCH, S.DEAD}),
    S.SYNCING: frozenset({S.PAPER, S.LIVE, S.HALTED, S.DISABLED, S.KILL_SWITCH, S.DEAD}),
    S.PAPER: frozenset({S.HALTED, S.SYNCING, S.DISABLED, S.KILL_SWITCH, S.DEAD}),
    S.LIVE: frozenset({S.HALTED, S.SYNCING, S.DISABLED, S.KILL_SWITCH, S.DEAD}),
    S.HALTED: frozenset({S.SYNCING, S.DISABLED, S.KILL_SWITCH, S.DEAD}),
    S.KILL_SWITCH: frozenset({S.DISABLED, S.DEAD}),
    S.DEAD: frozenset(),
}

OPEN_POSITION_STATES = frozenset({S.PAPER, S.LIVE})


class TransitionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LiveAuthorization:
    """Proof that the live lock passed. Minted only by promotion.live_lock."""

    policy_hash: str
    strategy_version: str
    issued_ms: int
    checks: tuple[str, ...]
    _token: object

    def __post_init__(self) -> None:
        from polymarket_bot.promotion.live_lock import _MINT_TOKEN  # noqa: PLC0415

        if self._token is not _MINT_TOKEN:
            raise TransitionError("LiveAuthorization can only be minted by the live lock")


@dataclass(frozen=True, slots=True)
class StateChange:
    from_state: BotState
    to_state: BotState
    reason: str
    manual_only: bool
    ts_ms: int
    component: str = "lifecycle"  # who requested the change (docs/incident-response.md)
    details: dict[str, Any] | None = None  # the exact condition, for halts and recoveries


Listener = Callable[[StateChange], None]


class BotStateMachine:
    def __init__(
        self,
        clock: Clock,
        *,
        initial: BotState = BotState.DISABLED,
        initial_reason: str = "startup",
        manual_only: bool = False,
    ) -> None:
        self._clock = clock
        self._state = initial
        self._reason = initial_reason
        self._manual_only = manual_only
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._live_auth: LiveAuthorization | None = None

    @property
    def state(self) -> BotState:
        return self._state

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def manual_only(self) -> bool:
        """True when the current HALTED/KILL_SWITCH state requires an operator."""
        return self._manual_only

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def can_open_positions(self) -> bool:
        return self._state in OPEN_POSITION_STATES

    def is_live(self) -> bool:
        return self._state is BotState.LIVE and self._live_auth is not None

    def transition(
        self,
        target: BotState,
        reason: str,
        *,
        manual: bool = False,
        manual_only: bool = False,
        live_authorization: LiveAuthorization | None = None,
        component: str = "lifecycle",
        details: dict[str, Any] | None = None,
    ) -> StateChange | None:
        """Move to ``target``. Returns None when already there (idempotent halts)."""
        with self._lock:
            current = self._state
            if target is current:
                if target in (BotState.HALTED, BotState.KILL_SWITCH) and manual_only:
                    self._manual_only = True  # escalate, never de-escalate silently
                return None
            if target not in ALLOWED_TRANSITIONS[current]:
                raise TransitionError(f"illegal transition {current} -> {target} ({reason})")
            if current is BotState.KILL_SWITCH and not manual:
                raise TransitionError("leaving KILL_SWITCH requires a manual operator reset")
            if current is BotState.HALTED and self._manual_only and not manual:
                raise TransitionError(f"HALTED ({self._reason}) requires operator intervention")
            if target is BotState.LIVE and live_authorization is None:
                raise TransitionError("entering LIVE requires a LiveAuthorization")
            self._live_auth = live_authorization if target is BotState.LIVE else None
            self._state = target
            self._reason = reason
            self._manual_only = (
                manual_only if target in (BotState.HALTED, BotState.KILL_SWITCH) else False
            )
            if target is BotState.KILL_SWITCH:
                self._manual_only = True
            change = StateChange(
                current,
                target,
                reason,
                self._manual_only,
                self._clock.now_ms(),
                component=component,
                details=details,
            )
        log.warning("bot state %s -> %s: %s", current, target, reason)
        for listener in self._listeners:
            listener(change)
        return change

    def halt(
        self,
        reason: str,
        *,
        manual_only: bool = False,
        component: str = "unknown",
        details: dict[str, Any] | None = None,
    ) -> StateChange | None:
        """Block new entries. Safe to call from any non-terminal state.

        ``component`` and ``details`` name the source and the exact condition;
        they are journaled as an explicit ``halt`` audit event.
        """
        with self._lock:
            if self._state in (BotState.KILL_SWITCH, BotState.DEAD, BotState.DISABLED):
                return None
            return self.transition(
                BotState.HALTED,
                reason,
                manual_only=manual_only,
                component=component,
                details=details,
            )
