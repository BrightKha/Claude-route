"""Global kill switch.

Engaged state is the OR of:
- the persisted flag in the state database, and
- the presence of the sentinel file ``<data_dir>/KILL_SWITCH`` (an operator can
  ``touch`` it from any shell to stop the bot, even if the process is wedged).

Engaging is always allowed. Resetting requires an explicit operator command
with a confirmation phrase; it is not reachable from the MCP server or the LLM.
After a reset the bot goes to DISABLED, never directly back to trading.
"""

from __future__ import annotations

import logging
from pathlib import Path

from polymarket_bot.audit.audit_log import AuditLog
from polymarket_bot.domain.clock import Clock
from polymarket_bot.domain.types import BotState
from polymarket_bot.lifecycle.state_machine import BotStateMachine
from polymarket_bot.storage.sqlite_store import StateStore

log = logging.getLogger(__name__)

RESET_CONFIRMATION_PHRASE = "I-HAVE-INVESTIGATED-AND-ACCEPT-RESET"
SENTINEL_NAME = "KILL_SWITCH"


class KillSwitchError(RuntimeError):
    pass


class KillSwitch:
    def __init__(
        self,
        data_dir: Path,
        store: StateStore,
        state_machine: BotStateMachine,
        audit: AuditLog,
        clock: Clock,
    ) -> None:
        self._sentinel = data_dir / SENTINEL_NAME
        self._store = store
        self._sm = state_machine
        self._audit = audit
        self._clock = clock

    @property
    def sentinel_path(self) -> Path:
        return self._sentinel

    def is_engaged(self) -> bool:
        engaged, _ = self._store.kill_switch_state()
        return engaged or self._sentinel.exists()

    def reason(self) -> str:
        engaged, reason = self._store.kill_switch_state()
        if engaged:
            return reason
        if self._sentinel.exists():
            text = self._sentinel.read_text(encoding="utf-8", errors="replace").strip()
            return text or "sentinel file present"
        return ""

    def engage(self, reason: str, *, source: str) -> None:
        now = self._clock.now_ms()
        already = self.is_engaged()
        self._store.set_kill_switch(engaged=True, reason=f"{source}: {reason}", ts_ms=now)
        try:
            self._sentinel.parent.mkdir(parents=True, exist_ok=True)
            self._sentinel.write_text(f"{now} {source}: {reason}\n", encoding="utf-8")
        except OSError:  # the DB flag alone is sufficient; never fail to engage
            log.exception("could not write kill switch sentinel file")
        self._audit.append("kill_switch_engaged", {"reason": reason, "source": source})
        self._store.insert_incident(
            now, "critical", "kill_switch", {"reason": reason, "source": source}
        )
        if self._sm.state not in (BotState.KILL_SWITCH, BotState.DEAD):
            self._sm.transition(BotState.KILL_SWITCH, f"kill switch: {reason}", manual_only=True)
        if not already:
            log.critical("KILL SWITCH ENGAGED by %s: %s", source, reason)

    def sync_from_persistence(self) -> None:
        """Called at startup: an engaged switch forces the KILL_SWITCH state."""
        if self.is_engaged() and self._sm.state is not BotState.KILL_SWITCH:
            self._sm.transition(
                BotState.KILL_SWITCH, f"kill switch persisted: {self.reason()}", manual_only=True
            )

    def reset(self, *, operator: str, confirmation: str, investigation_note: str) -> None:
        if confirmation != RESET_CONFIRMATION_PHRASE:
            raise KillSwitchError("wrong confirmation phrase; kill switch stays engaged")
        if len(investigation_note.strip()) < 10:
            raise KillSwitchError("an investigation note (>= 10 chars) is required")
        if not operator.strip():
            raise KillSwitchError("operator name is required")
        now = self._clock.now_ms()
        self._store.set_kill_switch(
            engaged=False, reason=f"reset: {investigation_note}", ts_ms=now, reset_by=operator
        )
        if self._sentinel.exists():
            self._sentinel.unlink()
        self._audit.append("kill_switch_reset", {"operator": operator, "note": investigation_note})
        if self._sm.state is BotState.KILL_SWITCH:
            self._sm.transition(BotState.DISABLED, f"kill switch reset by {operator}", manual=True)
