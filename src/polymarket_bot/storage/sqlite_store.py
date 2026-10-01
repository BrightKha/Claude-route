"""SQLite persistence for relational state (orders, fills, decisions, state).

Money and prices are stored as TEXT (Decimal strings) to avoid float drift.
WAL mode; every write is a short transaction. Order intents are written
*before* submission (write-ahead) so a crash can never lose track of an order.

A separate database (``inbox.sqlite``) holds MCP proposals, so the MCP process
can open the main database strictly read-only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from polymarket_bot.audit.jsonable import canonical_dumps
from polymarket_bot.domain.decisions import RiskDecision
from polymarket_bot.domain.orders import Fill, OrderIntent, OrderRecord
from polymarket_bot.domain.types import OrderPurpose, OrderStatus, OrderType, Side

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bot_state (
  id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL, reason TEXT NOT NULL,
  manual_only INTEGER NOT NULL DEFAULT 0, updated_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS state_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, from_state TEXT NOT NULL,
  to_state TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kill_switch (
  id INTEGER PRIMARY KEY CHECK (id = 1), engaged INTEGER NOT NULL, reason TEXT NOT NULL,
  engaged_ms INTEGER, reset_ms INTEGER, reset_by TEXT);
CREATE TABLE IF NOT EXISTS orders (
  intent_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, condition_id TEXT NOT NULL,
  market_slug TEXT NOT NULL, token_id TEXT NOT NULL, outcome TEXT NOT NULL, side TEXT NOT NULL,
  order_type TEXT NOT NULL, limit_price TEXT NOT NULL, buy_amount_usd TEXT, sell_shares TEXT,
  purpose TEXT NOT NULL, created_ms INTEGER NOT NULL, status TEXT NOT NULL,
  exchange_order_id TEXT, filled_shares TEXT NOT NULL, filled_notional TEXT NOT NULL,
  fees_usd TEXT NOT NULL, updated_ms INTEGER NOT NULL, last_error TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS orders_decision ON orders(decision_id);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status);
CREATE TABLE IF NOT EXISTS fills (
  fill_id TEXT PRIMARY KEY, intent_id TEXT, exchange_order_id TEXT, condition_id TEXT NOT NULL,
  token_id TEXT NOT NULL, outcome TEXT NOT NULL, side TEXT NOT NULL, price TEXT NOT NULL,
  shares TEXT NOT NULL, fee_usd TEXT NOT NULL, ts_ms INTEGER NOT NULL, liquidity TEXT NOT NULL,
  source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_decisions (
  decision_id TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL, allowed INTEGER NOT NULL,
  purpose TEXT NOT NULL, token_id TEXT NOT NULL, condition_id TEXT NOT NULL,
  candidate_id TEXT, policy_hash TEXT NOT NULL, risk_version TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL, condition_id TEXT NOT NULL,
  outcome TEXT NOT NULL, passes INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_reviews (
  review_id TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL, candidate_id TEXT, action TEXT NOT NULL,
  cost_usd TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, severity TEXT NOT NULL,
  kind TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reconciliation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, ok INTEGER NOT NULL,
  body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settlements (
  condition_id TEXT PRIMARY KEY, winning_outcome TEXT NOT NULL, resolved_ms INTEGER NOT NULL,
  source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equity_marks (
  ts_ms INTEGER PRIMARY KEY, equity TEXT NOT NULL, cash TEXT NOT NULL, exposure TEXT NOT NULL,
  daily_pnl TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS promotion_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, stage TEXT NOT NULL,
  action TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS price_to_beat_observations (
  slug TEXT PRIMARY KEY, window_start_ms INTEGER NOT NULL, official TEXT NOT NULL,
  stream TEXT, diff_bps REAL, first_seen_ms INTEGER NOT NULL, source TEXT NOT NULL,
  synthetic INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_status (
  name TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL, body TEXT NOT NULL);
"""

_INBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY, created_ms INTEGER NOT NULL, source TEXT NOT NULL,
  kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, status_reason TEXT,
  processed_ms INTEGER);
CREATE INDEX IF NOT EXISTS proposals_status ON proposals(status);
"""


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class StateStore:
    """Main state database. Thread-safe via a single lock (low write volume)."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        self._lock = threading.RLock()
        self._conn = _connect(path, read_only=read_only)
        if not read_only:
            with self._lock:
                # executescript() manages its own transaction; keep it outside _tx().
                self._conn.executescript(_SCHEMA)
            with self._tx() as cur:
                cur.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
        version = self.get_meta("schema_version")
        if version != str(SCHEMA_VERSION):
            raise RuntimeError(f"unsupported state schema version {version}")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        if self.read_only:
            raise PermissionError("state store opened read-only")
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----------------------------------------------------------------- meta
    def get_meta(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key = ?", (key,))
        return str(rows[0]["value"]) if rows else None

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------ bot state
    def save_bot_state(
        self, *, state: str, reason: str, manual_only: bool, ts_ms: int, from_state: str
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO bot_state(id, state, reason, manual_only, updated_ms) "
                "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET state = excluded.state, "
                "reason = excluded.reason, manual_only = excluded.manual_only, "
                "updated_ms = excluded.updated_ms",
                (state, reason, int(manual_only), ts_ms),
            )
            cur.execute(
                "INSERT INTO state_transitions(ts_ms, from_state, to_state, reason) "
                "VALUES (?, ?, ?, ?)",
                (ts_ms, from_state, state, reason),
            )

    def load_bot_state(self) -> tuple[str, str, bool] | None:
        rows = self._query("SELECT state, reason, manual_only FROM bot_state WHERE id = 1")
        if not rows:
            return None
        return str(rows[0]["state"]), str(rows[0]["reason"]), bool(rows[0]["manual_only"])

    def state_transitions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT ts_ms, from_state, to_state, reason FROM state_transitions "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ kill switch
    def set_kill_switch(
        self, *, engaged: bool, reason: str, ts_ms: int, reset_by: str | None = None
    ) -> None:
        with self._tx() as cur:
            if engaged:
                cur.execute(
                    "INSERT INTO kill_switch(id, engaged, reason, engaged_ms) VALUES (1, 1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET engaged = 1, reason = excluded.reason, "
                    "engaged_ms = excluded.engaged_ms, reset_ms = NULL, reset_by = NULL",
                    (reason, ts_ms),
                )
            else:
                cur.execute(
                    "INSERT INTO kill_switch(id, engaged, reason, reset_ms, reset_by) "
                    "VALUES (1, 0, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET engaged = 0, "
                    "reason = excluded.reason, reset_ms = excluded.reset_ms, "
                    "reset_by = excluded.reset_by",
                    (reason, ts_ms, reset_by),
                )

    def kill_switch_state(self) -> tuple[bool, str]:
        rows = self._query("SELECT engaged, reason FROM kill_switch WHERE id = 1")
        if not rows:
            return False, ""
        return bool(rows[0]["engaged"]), str(rows[0]["reason"])

    # ------------------------------------------------------------ orders
    def insert_order_intent(self, intent: OrderIntent) -> None:
        """Write-ahead record. Raises if the decision already produced an order."""
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO orders(intent_id, decision_id, condition_id, market_slug, token_id, "
                "outcome, side, order_type, limit_price, buy_amount_usd, sell_shares, purpose, "
                "created_ms, status, exchange_order_id, filled_shares, filled_notional, fees_usd, "
                "updated_ms, last_error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'0','0','0',?,NULL)",
                (
                    intent.intent_id,
                    intent.decision_id,
                    intent.condition_id,
                    intent.market_slug,
                    intent.token_id,
                    intent.outcome,
                    intent.side.value,
                    intent.order_type.value,
                    str(intent.limit_price),
                    None if intent.buy_amount_usd is None else str(intent.buy_amount_usd),
                    None if intent.sell_shares is None else str(intent.sell_shares),
                    intent.purpose.value,
                    intent.created_ms,
                    OrderStatus.INTENT.value,
                    intent.created_ms,
                ),
            )

    def update_order(self, record: OrderRecord) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE orders SET status = ?, exchange_order_id = ?, filled_shares = ?, "
                "filled_notional = ?, fees_usd = ?, updated_ms = ?, last_error = ? "
                "WHERE intent_id = ?",
                (
                    record.status.value,
                    record.exchange_order_id,
                    str(record.filled_shares),
                    str(record.filled_notional_usd),
                    str(record.fees_usd),
                    record.updated_ms,
                    record.last_error,
                    record.intent.intent_id,
                ),
            )
            if cur.rowcount != 1:
                raise KeyError(f"unknown order intent {record.intent.intent_id}")

    def load_orders(self, *, only_open: bool = False) -> list[OrderRecord]:
        sql = "SELECT * FROM orders"
        params: tuple[Any, ...] = ()
        if only_open:
            open_states = [s.value for s in OrderStatus if s.is_open]
            sql += f" WHERE status IN ({','.join('?' * len(open_states))})"
            params = tuple(open_states)
        return [_row_to_order(r) for r in self._query(sql + " ORDER BY created_ms", params)]

    # ------------------------------------------------------------ fills
    def insert_fill(self, fill: Fill) -> bool:
        """Idempotent: returns False if the fill id was already recorded."""
        with self._tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO fills(fill_id, intent_id, exchange_order_id, condition_id, "
                "token_id, outcome, side, price, shares, fee_usd, ts_ms, liquidity, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fill.fill_id,
                    fill.intent_id,
                    fill.exchange_order_id,
                    fill.condition_id,
                    fill.token_id,
                    fill.outcome,
                    fill.side.value,
                    str(fill.price),
                    str(fill.shares),
                    str(fill.fee_usd),
                    fill.ts_ms,
                    fill.liquidity,
                    fill.source,
                ),
            )
            return cur.rowcount == 1

    def load_fills(self) -> list[Fill]:
        return [
            Fill(
                fill_id=r["fill_id"],
                intent_id=r["intent_id"],
                exchange_order_id=r["exchange_order_id"],
                condition_id=r["condition_id"],
                token_id=r["token_id"],
                outcome=r["outcome"],
                side=Side(r["side"]),
                price=Decimal(r["price"]),
                shares=Decimal(r["shares"]),
                fee_usd=Decimal(r["fee_usd"]),
                ts_ms=int(r["ts_ms"]),
                liquidity=r["liquidity"],
                source=r["source"],
            )
            for r in self._query("SELECT * FROM fills ORDER BY ts_ms, fill_id")
        ]

    # ------------------------------------------------------------ decisions
    def insert_risk_decision(self, decision: RiskDecision) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO risk_decisions(decision_id, ts_ms, allowed, purpose, token_id, "
                "condition_id, candidate_id, policy_hash, risk_version, body) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.decision_id,
                    decision.timestamp_ms,
                    int(decision.allowed),
                    decision.purpose.value,
                    decision.token_id,
                    decision.condition_id,
                    decision.candidate_id,
                    decision.policy_hash,
                    decision.risk_version,
                    canonical_dumps(decision),
                ),
            )

    def insert_candidate(
        self,
        *,
        candidate_id: str,
        ts_ms: int,
        condition_id: str,
        outcome: str,
        passes: bool,
        body: object,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO candidates(candidate_id, ts_ms, condition_id, outcome, "
                "passes, body) VALUES (?,?,?,?,?,?)",
                (candidate_id, ts_ms, condition_id, outcome, int(passes), canonical_dumps(body)),
            )

    def insert_llm_review(
        self,
        *,
        review_id: str,
        ts_ms: int,
        candidate_id: str | None,
        action: str,
        cost_usd: Decimal,
        body: object,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO llm_reviews(review_id, ts_ms, candidate_id, action, cost_usd, body) "
                "VALUES (?,?,?,?,?,?)",
                (review_id, ts_ms, candidate_id, action, str(cost_usd), canonical_dumps(body)),
            )

    def llm_spend_since(self, since_ms: int) -> Decimal:
        rows = self._query("SELECT cost_usd FROM llm_reviews WHERE ts_ms >= ?", (since_ms,))
        return sum((Decimal(r["cost_usd"]) for r in rows), Decimal(0))

    # ------------------------------------------------------------ incidents etc.
    # ------------------------------------------------------------------ price-to-beat evidence
    def insert_ptb_observation(self, obs: dict[str, Any]) -> bool:
        """First observation of a window wins (live and replayed evidence never double count)."""
        with self._tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO price_to_beat_observations(slug, window_start_ms, official, "
                "stream, diff_bps, first_seen_ms, source, synthetic) VALUES (?,?,?,?,?,?,?,?)",
                (
                    obs["slug"],
                    obs["window_start_ms"],
                    str(obs["official"]),
                    None if obs["stream"] is None else str(obs["stream"]),
                    obs["diff_bps"],
                    obs["first_seen_ms"],
                    obs["source"],
                    int(bool(obs["synthetic"])),
                ),
            )
            return cur.rowcount == 1

    def ptb_observations(self, *, include_synthetic: bool = False) -> list[dict[str, Any]]:
        try:
            rows = self._query(
                "SELECT slug, window_start_ms, official, stream, diff_bps, first_seen_ms, source, "
                "synthetic FROM price_to_beat_observations WHERE synthetic <= ? "
                "ORDER BY window_start_ms",
                (int(include_synthetic),),
            )
        except sqlite3.OperationalError:  # database created before this table existed
            return []
        return [dict(r) for r in rows]

    def insert_incident(self, ts_ms: int, severity: str, kind: str, body: object) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO incidents(ts_ms, severity, kind, body) VALUES (?,?,?,?)",
                (ts_ms, severity, kind, canonical_dumps(body)),
            )

    def recent_incidents(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT ts_ms, severity, kind, body FROM incidents ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]

    def insert_reconciliation(self, ts_ms: int, ok: bool, body: object) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO reconciliation_runs(ts_ms, ok, body) VALUES (?,?,?)",
                (ts_ms, int(ok), canonical_dumps(body)),
            )

    def last_reconciliation(self) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT ts_ms, ok, body FROM reconciliation_runs ORDER BY id DESC LIMIT 1"
        )
        if not rows:
            return None
        return {"ts_ms": rows[0]["ts_ms"], "ok": bool(rows[0]["ok"]), **json.loads(rows[0]["body"])}

    def record_settlement(self, condition_id: str, outcome: str, ts_ms: int, source: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO settlements(condition_id, winning_outcome, resolved_ms, "
                "source) VALUES (?,?,?,?)",
                (condition_id, outcome, ts_ms, source),
            )

    def settlements(self) -> dict[str, str]:
        rows = self._query("SELECT condition_id, winning_outcome FROM settlements")
        return {r["condition_id"]: r["winning_outcome"] for r in rows}

    def insert_equity_mark(
        self, *, ts_ms: int, equity: Decimal, cash: Decimal, exposure: Decimal, daily_pnl: Decimal
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO equity_marks(ts_ms, equity, cash, exposure, daily_pnl) "
                "VALUES (?,?,?,?,?)",
                (ts_ms, str(equity), str(cash), str(exposure), str(daily_pnl)),
            )

    def latest_equity_mark(self) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM equity_marks ORDER BY ts_ms DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    def insert_promotion_event(
        self, *, ts_ms: int, stage: str, action: str, actor: str, body: object
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO promotion_events(ts_ms, stage, action, actor, body) "
                "VALUES (?,?,?,?,?)",
                (ts_ms, stage, action, actor, canonical_dumps(body)),
            )

    def promotion_events(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM promotion_events ORDER BY id")
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]

    # ------------------------------------------------------------ runtime status
    def publish_status(self, name: str, *, ts_ms: int, body: object) -> None:
        """Runtime snapshot documents read by the (read-only) MCP server and CLI."""
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO runtime_status(name, ts_ms, body) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET ts_ms = excluded.ts_ms, body = excluded.body",
                (name, ts_ms, canonical_dumps(body)),
            )

    def read_status(self, name: str) -> dict[str, Any] | None:
        rows = self._query("SELECT ts_ms, body FROM runtime_status WHERE name = ?", (name,))
        if not rows:
            return None
        return {"published_ms": rows[0]["ts_ms"], "data": json.loads(rows[0]["body"])}

    def recent_fills(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT fill_id, condition_id, token_id, outcome, side, price, shares, fee_usd, ts_ms, "
            "liquidity, source FROM fills ORDER BY ts_ms DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def recent_risk_decisions(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._query("SELECT body FROM risk_decisions ORDER BY ts_ms DESC LIMIT ?", (limit,))
        return [json.loads(r["body"]) for r in rows]


def _row_to_order(r: sqlite3.Row) -> OrderRecord:
    intent = OrderIntent(
        intent_id=r["intent_id"],
        decision_id=r["decision_id"],
        condition_id=r["condition_id"],
        market_slug=r["market_slug"],
        token_id=r["token_id"],
        outcome=r["outcome"],
        side=Side(r["side"]),
        order_type=OrderType(r["order_type"]),
        limit_price=Decimal(r["limit_price"]),
        buy_amount_usd=None if r["buy_amount_usd"] is None else Decimal(r["buy_amount_usd"]),
        sell_shares=None if r["sell_shares"] is None else Decimal(r["sell_shares"]),
        purpose=OrderPurpose(r["purpose"]),
        created_ms=int(r["created_ms"]),
    )
    return OrderRecord(
        intent=intent,
        status=OrderStatus(r["status"]),
        exchange_order_id=r["exchange_order_id"],
        filled_shares=Decimal(r["filled_shares"]),
        filled_notional_usd=Decimal(r["filled_notional"]),
        fees_usd=Decimal(r["fees_usd"]),
        updated_ms=int(r["updated_ms"]),
        last_error=r["last_error"],
    )


class ProposalInbox:
    """MCP proposal inbox (separate DB so the MCP process never writes main state)."""

    def __init__(self, path: Path) -> None:
        self._lock = threading.RLock()
        self._conn = _connect(path, read_only=False)
        with self._lock:
            self._conn.executescript(_INBOX_SCHEMA)

    def submit(
        self, *, proposal_id: str, created_ms: int, source: str, kind: str, payload: dict[str, Any]
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO proposals(proposal_id, created_ms, source, kind, payload, status) "
                "VALUES (?,?,?,?,?, 'PENDING')",
                (proposal_id, created_ms, source, kind, canonical_dumps(payload)),
            )

    def count_since(self, since_ms: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM proposals WHERE created_ms >= ?", (since_ms,)
            ).fetchone()
            return int(row["n"])

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM proposals WHERE status = 'PENDING' ORDER BY created_ms"
            ).fetchall()
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def mark(self, proposal_id: str, status: str, reason: str, ts_ms: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE proposals SET status = ?, status_reason = ?, processed_ms = ? "
                "WHERE proposal_id = ?",
                (status, reason, ts_ms, proposal_id),
            )

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            return None if row is None else {**dict(row), "payload": json.loads(row["payload"])}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
