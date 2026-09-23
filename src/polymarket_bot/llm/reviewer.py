"""Deterministic policy around Claude reviews.

Claude is consulted *off* the decision path: :meth:`CandidateReviewer.verdict_for`
is a pure cache lookup used on every decision tick, and
:meth:`CandidateReviewer.run_review` performs the (slow) call in a background
task. While a review is pending the candidate is not traded, and exits are never
delayed by a review.

Mapping (docs/trading.md "Claude review"):

* ``mode=off``                       -> deterministic decision only.
* REJECT, refusal, invalid/truncated -> not allowed (cached for ``cache_ttl_s``).
* APPROVE (confidence >= threshold)  -> allowed with the candidate *tightened* by
  any lower probability override; valid for ``approval_ttl_s`` and only while the
  executable price stays within ``max_price_move_since_review``.
* NO_OP, low-confidence APPROVE, timeout, error, budget exhausted, no API key
  -> "not reviewed": allowed only in advisory mode with
  ``allow_trading_without_llm=true`` (never in ``required`` mode).
* A prompt that matches a secret pattern is never sent and blocks the trade.

Nothing here can increase size, relax a limit or bypass the Risk Engine: the
(possibly tightened) candidate still goes through the Risk Engine.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from polymarket_bot.audit.audit_log import AuditLog
from polymarket_bot.config.app_config import LLMConfig
from polymarket_bot.domain.clock import Clock
from polymarket_bot.domain.decisions import TradeCandidate
from polymarket_bot.llm.budget import LLMBudget, worst_case_cost_usd
from polymarket_bot.llm.schemas import (
    ClaudeCallResult,
    LLMReview,
    ReviewContext,
    tighten_candidate,
)
from polymarket_bot.storage.sqlite_store import StateStore

log = logging.getLogger(__name__)

VerdictStatus = Literal["llm_off", "approved", "rejected", "not_reviewed", "pending", "needs_call"]


class ReviewClient(Protocol):
    @property
    def available(self) -> bool: ...

    def prompt_bytes(self, context: ReviewContext) -> int: ...

    async def review(
        self, context: ReviewContext, *, reserved_cost_usd: Decimal
    ) -> ClaudeCallResult: ...


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    status: VerdictStatus
    allowed: bool
    candidate: TradeCandidate  # tightened when an approval carried an override
    reasons: tuple[str, ...]
    review_id: str | None = None
    max_holding_s: int | None = None


@dataclass(frozen=True, slots=True)
class _Entry:
    kind: Literal["approve", "reject", "not_reviewed"]
    ts_ms: int
    reviewed_price: Decimal
    review: LLMReview | None
    review_id: str | None
    reason: str


def market_key(candidate: TradeCandidate) -> str:
    return f"{candidate.condition_id}:{candidate.token_id}"


class CandidateReviewer:
    def __init__(
        self,
        cfg: LLMConfig,
        client: ReviewClient | None,
        budget: LLMBudget,
        *,
        store: StateStore,
        audit: AuditLog,
        clock: Clock,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._budget = budget
        self._store = store
        self._audit = audit
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._in_flight: set[str] = set()

    # ------------------------------------------------------------------ policy
    def _without_review(self, candidate: TradeCandidate, reason: str) -> ReviewVerdict:
        allowed = self._cfg.mode == "advisory" and self._cfg.allow_trading_without_llm
        return ReviewVerdict("not_reviewed", allowed, candidate, (reason,))

    def verdict_for(self, candidate: TradeCandidate, now_ms: int) -> ReviewVerdict:
        """Pure lookup; never calls Claude."""
        cfg = self._cfg
        if cfg.mode == "off":
            return ReviewVerdict("llm_off", True, candidate, ("llm review disabled",))
        key = market_key(candidate)
        if key in self._in_flight:
            return ReviewVerdict("pending", False, candidate, ("review in progress",))
        entry = self._entries.get(key)
        if entry is not None:
            age_ms = now_ms - entry.ts_ms
            if entry.kind == "reject" and age_ms <= cfg.cache_ttl_s * 1000:
                return ReviewVerdict(
                    "rejected", False, candidate, (entry.reason,), review_id=entry.review_id
                )
            if entry.kind == "not_reviewed" and age_ms <= cfg.cache_ttl_s * 1000:
                return self._without_review(candidate, entry.reason)
            if entry.kind == "approve" and age_ms <= cfg.approval_ttl_s * 1000:
                moved = abs(candidate.executable_price - entry.reviewed_price)
                if moved <= cfg.max_price_move_since_review:
                    assert entry.review is not None
                    return ReviewVerdict(
                        "approved",
                        True,
                        tighten_candidate(candidate, entry.review),
                        ("approved by llm review",),
                        review_id=entry.review_id,
                        max_holding_s=entry.review.max_holding_time_seconds,
                    )
        if candidate.conservative_edge < float(cfg.review_min_conservative_edge):
            return self._without_review(candidate, "edge below review threshold")
        if self._client is None or not self._client.available:
            return self._without_review(candidate, "llm client unavailable")
        return ReviewVerdict("needs_call", False, candidate, ("review required",))

    # ------------------------------------------------------------------ calls
    async def run_review(self, candidate: TradeCandidate, context: ReviewContext) -> None:
        """Perform one review (budget permitting) and cache its outcome."""
        key = market_key(candidate)
        if self._client is None or key in self._in_flight:
            return
        now = self._clock.now_ms()
        prompt_bytes = self._client.prompt_bytes(context)
        blocked = self._budget.blocked_reason(now, key, prompt_bytes)
        if blocked is not None:
            self._store_entry(
                key, "not_reviewed", now, candidate, review=None, review_id=None, reason=blocked
            )
            return
        self._in_flight.add(key)
        try:
            reserved = worst_case_cost_usd(self._cfg, prompt_bytes)
            result = await self._client.review(context, reserved_cost_usd=reserved)
        finally:
            self._in_flight.discard(key)
        done = self._clock.now_ms()
        self._budget.record_call(done, key, result.cost_usd)
        review_id = f"rv-{uuid.uuid4().hex[:16]}"
        self._persist(review_id, done, candidate, context, result)
        kind, reason = self._classify(result)
        self._store_entry(
            key, kind, done, candidate, review=result.review, review_id=review_id, reason=reason
        )

    def _classify(
        self, result: ClaudeCallResult
    ) -> tuple[Literal["approve", "reject", "not_reviewed"], str]:
        if result.outcome == "ok":
            review = result.review
            assert review is not None
            if review.action == "REJECT":
                return "reject", "rejected by llm review"
            if review.action == "APPROVE" and review.confidence >= self._cfg.min_approve_confidence:
                return "approve", "approved by llm review"
            return "not_reviewed", f"llm {review.action} (confidence {review.confidence:.2f})"
        if result.outcome in ("refusal", "invalid_output", "truncated"):
            return "reject", f"llm {result.outcome} treated as REJECT"
        if result.outcome == "blocked":
            log.critical("review prompt matched a secret pattern; not sent")
            return "reject", "prompt blocked by secret guard"
        return "not_reviewed", f"llm {result.outcome}: {result.detail}"

    def _store_entry(
        self,
        key: str,
        kind: Literal["approve", "reject", "not_reviewed"],
        ts_ms: int,
        candidate: TradeCandidate,
        *,
        review: LLMReview | None,
        review_id: str | None,
        reason: str,
    ) -> None:
        self._entries[key] = _Entry(
            kind, ts_ms, candidate.executable_price, review, review_id, reason
        )

    def _persist(
        self,
        review_id: str,
        ts_ms: int,
        candidate: TradeCandidate,
        context: ReviewContext,
        result: ClaudeCallResult,
    ) -> None:
        body = {
            "context": context.model_dump(mode="json"),
            "outcome": result.outcome,
            "review": None if result.review is None else result.review.model_dump(mode="json"),
            "model": result.model,
            "usage": result.usage,
            "latency_ms": result.latency_ms,
            "fallback_used": result.fallback_used,
            "prompt_version": result.prompt_version,
            "detail": result.detail,
        }
        action = result.review.action if result.review else result.outcome.upper()
        self._store.insert_llm_review(
            review_id=review_id,
            ts_ms=ts_ms,
            candidate_id=candidate.candidate_id,
            action=action,
            cost_usd=result.cost_usd,
            body=body,
        )
        self._audit.append(
            "llm_review",
            {"review_id": review_id, "candidate_id": candidate.candidate_id, **body},
        )

    @property
    def spent_today_usd(self) -> Decimal:
        return self._budget.spent_today_usd
