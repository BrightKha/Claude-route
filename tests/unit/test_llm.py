"""Claude review: schema, budget, SDK client (mock transport), reviewer policy."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from polymarket_bot.audit.audit_log import AuditLog
from polymarket_bot.config.app_config import LLMConfig
from polymarket_bot.domain.clock import SimulatedClock
from polymarket_bot.llm.budget import LLMBudget, TokenUsage, usage_cost_usd, worst_case_cost_usd
from polymarket_bot.llm.claude_client import FALLBACK_BETA, ClaudeReviewClient
from polymarket_bot.llm.reviewer import CandidateReviewer
from polymarket_bot.llm.schemas import (
    REVIEW_JSON_SCHEMA,
    ClaudeCallResult,
    LLMReview,
    ReviewContext,
    build_review_context,
    tighten_candidate,
)
from polymarket_bot.security.secrets import Secret
from polymarket_bot.storage.sqlite_store import StateStore
from tests.factories import T0, make_candidate, make_snapshot

D = Decimal
NOW = T0 + 120_000
ANT_PREFIX = "sk-" + "ant-"


def _review(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": "APPROVE",
        "fair_probability_override": None,
        "confidence": 0.8,
        "thesis": "price to beat verified, sources agree",
        "key_risks": ["vol spike"],
        "invalidators": ["spot crosses price to beat"],
        "recommended_exit_conditions": ["exit if bid below fair"],
        "max_holding_time_seconds": None,
    }
    base.update(kw)
    return base


def _context() -> ReviewContext:
    return build_review_context(make_candidate(), make_snapshot(), volatility_bps_per_sqrt_s=1.2)


# ---------------------------------------------------------------- schema
def test_wire_schema_matches_pydantic_model() -> None:
    assert set(REVIEW_JSON_SCHEMA["properties"]) == set(LLMReview.model_fields)
    assert set(REVIEW_JSON_SCHEMA["required"]) == set(LLMReview.model_fields)
    assert REVIEW_JSON_SCHEMA["additionalProperties"] is False


@pytest.mark.parametrize(
    "bad",
    [
        {"confidence": 1.5},
        {"fair_probability_override": -0.1},
        {"action": "BUY"},
        {"max_holding_time_seconds": 3600},
        {"size_usd": 100},  # extra fields are refused: Claude cannot size
        {"key_risks": ["x"] * 11},
    ],
)
def test_review_validation_fails_closed(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LLMReview.model_validate(_review(**bad))


def test_context_is_an_explicit_whitelist() -> None:
    fields = set(ReviewContext.model_fields)
    for forbidden in ("wallet", "balance", "cash", "key", "secret", "order_id", "policy"):
        assert not any(forbidden in f for f in fields), forbidden
    ctx = _context()
    assert ctx.price_to_beat_verified is True
    assert ctx.side == "BUY"


def test_upward_override_is_ignored() -> None:
    c = make_candidate()
    assert tighten_candidate(c, LLMReview(**_review(fair_probability_override=0.95))) == c


@given(st.floats(min_value=0.0, max_value=1.0))
def test_override_can_only_reduce_edge(override: float) -> None:
    c = make_candidate()
    t = tighten_candidate(c, LLMReview(**_review(fair_probability_override=override)))
    assert t.conservative_edge <= c.conservative_edge
    assert t.worst_case_edge <= c.worst_case_edge
    assert t.probability_lower <= c.probability_lower
    assert t.fair_probability <= c.fair_probability
    assert (t.notional_usd, t.max_allowed_size_usd, t.size_shares) == (
        c.notional_usd,
        c.max_allowed_size_usd,
        c.size_shares,
    )


# ---------------------------------------------------------------- budget
def _cfg(**kw: Any) -> LLMConfig:
    return LLMConfig.model_validate(kw)


def test_cost_from_usage() -> None:
    cfg = _cfg()
    cost = usage_cost_usd(cfg, TokenUsage(1_000_000, 100_000, 0, 1_000_000))
    assert cost == D("5") + D("2.5") + D("0.5")


def test_rate_limits_and_debounce() -> None:
    b = LLMBudget(
        _cfg(max_calls_per_minute=2, max_calls_per_hour=3), spent_today_usd=D(0), now_ms=NOW
    )
    assert b.blocked_reason(NOW, "m1", 1000) is None
    b.record_call(NOW, "m1", D("0.01"))
    assert "debounced" in (b.blocked_reason(NOW + 1000, "m1", 1000) or "")
    b.record_call(NOW + 1000, "m2", D("0.01"))
    assert "minute" in (b.blocked_reason(NOW + 2000, "m3", 1000) or "")
    b.record_call(NOW + 61_000, "m3", D("0.01"))
    assert "hour" in (b.blocked_reason(NOW + 122_000, "m4", 1000) or "")
    assert b.blocked_reason(NOW + 3_600_001, "m4", 1000) is None


def test_daily_budget_includes_worst_case_of_next_call() -> None:
    cfg = _cfg(max_spend_per_day_usd=D("1"))
    worst = worst_case_cost_usd(cfg, 5000)
    b = LLMBudget(cfg, spent_today_usd=D("1") - worst + D("0.0001"), now_ms=NOW)
    assert "budget" in (b.blocked_reason(NOW, "m", 5000) or "")
    tomorrow = NOW + 86_400_000
    assert b.blocked_reason(tomorrow, "m", 5000) is None
    assert b.spent_today_usd == 0


def test_worst_case_doubles_with_server_side_fallback() -> None:
    on = worst_case_cost_usd(_cfg(server_side_fallback=True), 1000)
    off = worst_case_cost_usd(_cfg(server_side_fallback=False), 1000)
    assert on == 2 * off


# ---------------------------------------------------------------- SDK client
def _message(text: str, stop: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 300},
    }


class Recorder:
    def __init__(self, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = body
        self.requests: list[httpx2.Request] = []
        self.raise_timeout = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.raise_timeout:
            raise httpx2.ReadTimeout("slow", request=request)
        return httpx2.Response(self.status, json=self.body or {"type": "error"})


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = ANT_PREFIX + "api03-" + "t" * 40
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    return key


def _client(rec: Recorder, **cfg: Any) -> ClaudeReviewClient:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(rec))
    return ClaudeReviewClient(_cfg(**cfg), http_client=http)


async def test_request_uses_structured_output_and_fallback(api_key: str) -> None:
    rec = Recorder(body=_message(json.dumps(_review())))
    result = await _client(rec).review(_context(), reserved_cost_usd=D("0.1"))
    assert result.outcome == "ok"
    assert result.review is not None and result.review.action == "APPROVE"
    assert result.cost_usd == D("1200") * 5 / 1_000_000 + D("300") * 25 / 1_000_000
    (req,) = rec.requests
    body = json.loads(req.content)
    assert body["model"] == "claude-opus-5"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": REVIEW_JSON_SCHEMA}
    assert body["output_config"]["effort"] == "low"
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in req.headers.get("anthropic-beta", "")
    assert req.headers["x-api-key"] == api_key
    assert api_key not in req.content.decode()  # the key only travels in the header


async def test_refusal_is_not_an_approval(api_key: str) -> None:
    rec = Recorder(body=_message("", stop="refusal"))
    result = await _client(rec).review(_context(), reserved_cost_usd=D("0.1"))
    assert result.outcome == "refusal" and result.review is None


@pytest.mark.parametrize(
    ("text", "stop", "outcome"),
    [
        ("not json", "end_turn", "invalid_output"),
        (json.dumps(_review(confidence=2)), "end_turn", "invalid_output"),
        ('{"action": "APPROVE"', "max_tokens", "truncated"),
    ],
)
async def test_bad_outputs_fail_closed(api_key: str, text: str, stop: str, outcome: str) -> None:
    rec = Recorder(body=_message(text, stop=stop))
    result = await _client(rec).review(_context(), reserved_cost_usd=D("0.1"))
    assert result.outcome == outcome and result.review is None


async def test_http_error_books_reserved_cost(api_key: str) -> None:
    rec = Recorder(
        status=500, body={"type": "error", "error": {"type": "api_error", "message": "x"}}
    )
    result = await _client(rec).review(_context(), reserved_cost_usd=D("0.1"))
    assert result.outcome == "error" and result.cost_usd == D("0.1")
    assert len(rec.requests) == 1  # max_retries=0: no silent retry


async def test_timeout(api_key: str) -> None:
    rec = Recorder()
    rec.raise_timeout = True
    result = await _client(rec).review(_context(), reserved_cost_usd=D("0.1"))
    assert result.outcome == "timeout" and result.cost_usd == D("0.1")


async def test_no_key_means_unavailable_and_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rec = Recorder()
    client = _client(rec)
    assert not client.available
    result = await client.review(_context(), reserved_cost_usd=D("0.1"))
    assert result.outcome == "unavailable" and rec.requests == []


async def test_prompt_with_secret_is_never_sent(api_key: str) -> None:
    leaked = "0x" + "ab" * 32
    Secret(leaked)  # registers the value for redaction, like a loaded key would
    ctx = replace_field(_context(), question=f"will {leaked} rise?")
    rec = Recorder(body=_message(json.dumps(_review())))
    result = await _client(rec).review(ctx, reserved_cost_usd=D("0.1"))
    assert result.outcome == "blocked" and rec.requests == []


def replace_field(ctx: ReviewContext, **kw: Any) -> ReviewContext:
    return ctx.model_copy(update=kw)


# ---------------------------------------------------------------- reviewer policy
class FakeClient:
    def __init__(self, result: ClaudeCallResult | None = None) -> None:
        self.available = True
        self.calls = 0
        self.result = result or ok(_review())

    def prompt_bytes(self, context: ReviewContext) -> int:
        return 4000

    async def review(
        self, context: ReviewContext, *, reserved_cost_usd: Decimal
    ) -> ClaudeCallResult:
        self.calls += 1
        return self.result


def ok(review: dict[str, Any]) -> ClaudeCallResult:
    return ClaudeCallResult(
        "ok", LLMReview(**review), D("0.01"), TokenUsage(1000, 200), "claude-opus-5", 900, False, ""
    )


def failed(outcome: Any) -> ClaudeCallResult:
    return ClaudeCallResult(outcome, None, D("0.02"), None, None, 0, False, "x")


class Env:
    def __init__(self, tmp: Path, client: FakeClient | None, **cfg: Any) -> None:
        self.clock = SimulatedClock(NOW)
        self.cfg = _cfg(**cfg)
        self.store = StateStore(tmp / "s.sqlite")
        self.audit = AuditLog(tmp / "a.jsonl", self.clock, fsync=False)
        self.client = client
        self.reviewer = CandidateReviewer(
            self.cfg,
            client,
            LLMBudget(self.cfg, spent_today_usd=D(0), now_ms=NOW),
            store=self.store,
            audit=self.audit,
            clock=self.clock,
        )

    async def review(self, candidate: Any = None) -> Any:
        c = candidate or make_candidate(conservative_edge=0.06)
        v = self.reviewer.verdict_for(c, self.clock.now_ms())
        if v.status == "needs_call":
            await self.reviewer.run_review(c, _context())
            v = self.reviewer.verdict_for(c, self.clock.now_ms())
        return v


async def test_mode_off_is_deterministic_only(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeClient(), mode="off")
    v = await env.review()
    assert v.status == "llm_off" and v.allowed
    assert env.client is not None and env.client.calls == 0


@pytest.mark.parametrize(
    ("mode", "allow", "allowed"),
    [("advisory", True, True), ("advisory", False, False), ("required", True, False)],
)
async def test_unreviewed_policy(tmp_path: Path, mode: str, allow: bool, allowed: bool) -> None:
    env = Env(tmp_path, FakeClient(), mode=mode, allow_trading_without_llm=allow)
    v = await env.review(make_candidate(conservative_edge=0.01))  # below review threshold
    assert v.status == "not_reviewed" and v.allowed is allowed
    assert env.client is not None and env.client.calls == 0


async def test_approval_tightens_and_expires(tmp_path: Path) -> None:
    client = FakeClient(ok(_review(fair_probability_override=0.70, max_holding_time_seconds=90)))
    env = Env(tmp_path, client)
    base = make_candidate(conservative_edge=0.06)
    v = await env.review(base)
    assert v.status == "approved" and v.allowed
    assert v.candidate.conservative_edge == pytest.approx(0.06 - (0.72 - 0.70))
    assert v.max_holding_s == 90
    assert env.store.llm_spend_since(0) == D("0.01")
    # Price moved too much since the review: not approved any more.
    moved = replace(base, executable_price=base.executable_price + D("0.03"))
    assert env.reviewer.verdict_for(moved, NOW).status == "needs_call"
    # Approval expires.
    env.clock.advance_to(NOW + int(env.cfg.approval_ttl_s * 1000) + 1)
    assert env.reviewer.verdict_for(base, env.clock.now_ms()).status == "needs_call"


async def test_rejection_sticks_for_cache_ttl(tmp_path: Path) -> None:
    client = FakeClient(ok(_review(action="REJECT")))
    env = Env(tmp_path, client)
    v = await env.review()
    assert v.status == "rejected" and not v.allowed
    env.clock.advance_to(NOW + 30_000)
    assert (await env.review()).status == "rejected"
    assert client.calls == 1


@pytest.mark.parametrize("outcome", ["refusal", "invalid_output", "truncated", "blocked"])
async def test_model_failures_are_rejections(tmp_path: Path, outcome: str) -> None:
    env = Env(tmp_path, FakeClient(failed(outcome)))
    v = await env.review()
    assert v.status == "rejected" and not v.allowed


@pytest.mark.parametrize(("mode", "allowed"), [("advisory", True), ("required", False)])
async def test_timeout_falls_back_to_policy(tmp_path: Path, mode: str, allowed: bool) -> None:
    env = Env(tmp_path, FakeClient(failed("timeout")), mode=mode)
    v = await env.review()
    assert v.status == "not_reviewed" and v.allowed is allowed
    assert env.store.llm_spend_since(0) == D("0.02")  # reserved cost booked


async def test_low_confidence_approval_is_not_an_approval(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeClient(ok(_review(confidence=0.3))), mode="required")
    v = await env.review()
    assert v.status == "not_reviewed" and not v.allowed


async def test_budget_exhaustion_prevents_calls(tmp_path: Path) -> None:
    client = FakeClient()
    env = Env(tmp_path, client, max_spend_per_day_usd=D("0.0001"), mode="required")
    v = await env.review()
    assert client.calls == 0
    assert v.status == "not_reviewed" and not v.allowed
    assert "budget" in v.reasons[0]


async def test_reviews_are_persisted_and_audited(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeClient())
    await env.review()
    text = env.audit.path.read_text()
    assert '"llm_review"' in text and "claude-opus-5" in text
