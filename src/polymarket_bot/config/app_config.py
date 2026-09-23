"""Application configuration (YAML). Strict: unknown keys are rejected.

Secrets are NOT part of this model; see ``config/settings.py`` and
``security/secrets.py``.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from polymarket_bot.config.risk_policy import RiskPolicy


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketDataConfig(_Strict):
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    clob_market_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    rtds_ws_url: str = "wss://ws-live-data.polymarket.com"
    series_id: str = "10684"
    series_slug: str = "btc-up-or-down-5m"
    rest_timeout_s: float = Field(5.0, gt=0, le=30)
    rest_max_retries: int = Field(2, ge=0, le=5)
    clob_ping_interval_s: float = Field(10.0, gt=0)
    clob_silence_timeout_s: float = Field(30.0, gt=0)
    rtds_ping_interval_s: float = Field(5.0, gt=0)
    rtds_silence_timeout_s: float = Field(15.0, gt=0)
    reconnect_base_s: float = Field(1.0, gt=0)
    reconnect_max_s: float = Field(30.0, gt=0)
    reconnect_jitter: float = Field(0.25, ge=0, le=1)
    discovery_interval_s: float = Field(30.0, gt=0)
    discovery_lookahead_s: int = Field(900, gt=0, description="Track markets starting within")
    book_resync_interval_s: float = Field(60.0, gt=0)
    depth_window: Decimal = Field(Decimal("0.05"), gt=0, description="Price window for depth")
    reference_symbol: str = "btc/usd"
    secondary_symbol: str | None = Field("btcusdt", description="Binance symbol for dispersion")
    max_source_dispersion_bps: float = Field(50.0, gt=0)


class FairValueConfig(_Strict):
    model_version: str = "twap-gauss-1.0.0"
    twap_lookback_s: int = Field(60, gt=0, description="Must equal the market's rule")
    vol_halflife_s: float = Field(300.0, gt=0)
    vol_min_samples: int = Field(60, ge=10)
    vol_floor_bps_per_sqrt_s: float = Field(0.5, gt=0)
    vol_cap_bps_per_sqrt_s: float = Field(20.0, gt=0)
    vol_uncertainty_mult: float = Field(0.30, ge=0, le=2)
    model_error_bps: float = Field(3.0, ge=0, description="TWAP reproduction + basis error")
    price_to_beat_tolerance_bps: float = Field(2.0, ge=0)
    calibrator_path: str | None = None


class EdgeConfig(_Strict):
    signal_version: str = "btc5m-edge-1.0.0"
    target_order_usd: Decimal = Field(Decimal("10"), gt=0)
    slippage_buffer: Decimal = Field(Decimal("0.005"), ge=0, description="Per-share price buffer")
    early_exit_probability: Decimal = Field(Decimal("0.5"), ge=0, le=1)
    min_confidence: float = Field(0.0, ge=0, le=1)


class ExitPolicyConfig(_Strict):
    """Explicit, testable exit rules (docs/trading.md "Exit policy")."""

    convergence_tolerance: Decimal = Field(Decimal("0.01"), ge=0)
    take_profit_per_share: Decimal = Field(Decimal("0.20"), gt=0)
    invalidation_margin: Decimal = Field(Decimal("0.05"), ge=0)
    invalidation_exit_mode: Literal["if_not_worse_than_fair", "always"] = "if_not_worse_than_fair"
    max_holding_time_s: int | None = Field(None, gt=0)
    hold_to_resolution: bool = True
    no_exit_window_s: int = Field(8, ge=0, description="No exits in the final seconds")
    risk_exit_discount: Decimal = Field(Decimal("0.02"), ge=0)
    min_exit_price: Decimal = Field(Decimal("0.01"), gt=0, lt=1)
    exit_stale_data_ms: int = Field(3000, gt=0)
    on_halt: Literal["hold", "exit_if_priced"] = "exit_if_priced"
    on_kill_switch: Literal["hold", "exit_if_priced"] = "exit_if_priced"


class ExecutionConfig(_Strict):
    entry_order_type: Literal["FAK"] = "FAK"
    exit_order_type: Literal["FAK"] = "FAK"
    submit_timeout_s: float = Field(5.0, gt=0, le=30)
    unknown_state_timeout_s: float = Field(20.0, gt=0)
    max_in_flight_orders: int = Field(1, ge=1, le=3)


class PaperExchangeConfig(_Strict):
    initial_balance_usd: Decimal = Field(Decimal("200"), gt=0)
    latency_ms: int = Field(150, ge=0)
    latency_jitter_ms: int = Field(100, ge=0)
    taker_delay_ms: int = Field(50, ge=0, description="Polymarket crypto taker delay")
    seed: int = 7
    reject_probability: float = Field(0.0, ge=0, le=1, description="Chaos testing only")
    liquidity_replenish_ms: int = Field(
        2000, ge=0, description="Our simulated fills hide displayed size for this long"
    )
    fee_multiplier: Decimal = Field(
        Decimal(1), ge=1, le=5, description="Stress testing only: scale simulated fees up"
    )


class LLMConfig(_Strict):
    mode: Literal["off", "advisory", "required"] = "advisory"
    model: str = "claude-opus-5"
    effort: Literal["low", "medium", "high"] = "low"
    max_tokens: int = Field(2000, gt=0, le=16000)
    timeout_s: float = Field(30.0, gt=0, le=120)
    server_side_fallback: bool = True
    review_min_conservative_edge: Decimal = Field(Decimal("0.05"), ge=0)
    max_calls_per_minute: int = Field(2, ge=0)
    max_calls_per_hour: int = Field(20, ge=0)
    max_spend_per_day_usd: Decimal = Field(Decimal("5"), ge=0)
    input_price_per_mtok_usd: Decimal = Field(Decimal("5"), ge=0)
    output_price_per_mtok_usd: Decimal = Field(Decimal("25"), ge=0)
    approval_ttl_s: float = Field(20.0, gt=0)
    cache_ttl_s: float = Field(60.0, ge=0)
    debounce_per_market_s: float = Field(60.0, ge=0)
    max_price_move_since_review: Decimal = Field(Decimal("0.02"), ge=0)
    allow_trading_without_llm: bool = True
    min_approve_confidence: float = Field(0.5, ge=0, le=1, description="Lower => NO_OP")
    max_retries: int = Field(0, ge=0, le=2, description="SDK retries; decisions are time-bound")
    cache_write_price_multiplier: Decimal = Field(Decimal("1.25"), ge=1)
    cache_read_price_multiplier: Decimal = Field(Decimal("0.1"), ge=0)


class WatchdogConfig(_Strict):
    check_interval_s: float = Field(1.0, gt=0)
    loop_stall_s: float = Field(5.0, gt=0)
    max_market_data_silence_s: float = Field(10.0, gt=0)
    max_reference_silence_s: float = Field(10.0, gt=0)
    max_reconciliation_age_s: float = Field(180.0, gt=0)
    max_clock_drift_ms: int = Field(1000, gt=0)
    max_auto_recoveries_per_hour: int = Field(6, ge=0)
    cancel_orders_on_halt: bool = True


class ReconciliationConfig(_Strict):
    interval_s: float = Field(60.0, gt=0)
    share_tolerance: Decimal = Field(Decimal("0.0001"), ge=0)
    balance_tolerance_usd: Decimal = Field(Decimal("0.01"), ge=0)


class StrategyConfig(_Strict):
    name: Literal["btc_5m"] = "btc_5m"
    enabled: bool = True
    version: str = "btc5m-0.1.0"
    enabled_rule_ids: tuple[str, ...] = ("btc_5m_twap60_v3",)
    decision_interval_ms: int = Field(1000, ge=100)
    outcomes: tuple[str, str] = ("Up", "Down")


class McpConfig(_Strict):
    max_proposals_per_minute: int = Field(2, ge=0)
    proposal_ttl_s: int = Field(30, gt=0)
    mcp_trade_requires_deterministic_edge: Literal[True] = True  # cannot be disabled
    http_enabled: bool = False
    http_bind: str = "127.0.0.1"
    http_port: int = Field(8765, gt=1024, lt=65536)


class MonitoringConfig(_Strict):
    metrics_enabled: bool = False
    metrics_bind: str = "127.0.0.1"
    metrics_port: int = Field(9108, gt=1024, lt=65536)


class ComplianceConfig(_Strict):
    # Source: help.polymarket.com geographic restrictions (2026-08-14), docs/research.md §6.
    blocked_countries: tuple[str, ...] = (
        "AU", "BE", "BY", "BR", "BI", "CF", "CD", "CU", "DE", "ET", "FR", "GB", "IE", "IR",
        "IQ", "IT", "JP", "KP", "LB", "LY", "MM", "MT", "NI", "NL", "NZ", "PL", "RU", "SG",
        "SK", "SO", "SS", "SD", "SY", "TW", "TH", "UM", "US", "VE", "YE", "ZW",
    )  # fmt: skip
    close_only_countries: tuple[str, ...] = ("SG", "PL", "TH", "TW")
    blocked_regions_note: str = "CA-AB, CA-BC, CA-ON, CA-QC, UA-43, UA-14, UA-09"
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    require_geoblock_endpoint_for_live: bool = True


class AppConfig(_Strict):
    mode: Literal["disabled", "paper", "replay", "live"] = "disabled"
    data_dir: Path = Path("./data")
    promotion_stage_required_for_live: Literal["SMALL_LIVE", "LIVE"] = "SMALL_LIVE"
    risk: RiskPolicy = RiskPolicy()
    market_data: MarketDataConfig = MarketDataConfig()
    fair_value: FairValueConfig = FairValueConfig()
    edge: EdgeConfig = EdgeConfig()
    exits: ExitPolicyConfig = ExitPolicyConfig()
    execution: ExecutionConfig = ExecutionConfig()
    paper: PaperExchangeConfig = PaperExchangeConfig()
    llm: LLMConfig = LLMConfig()
    watchdog: WatchdogConfig = WatchdogConfig()
    reconciliation: ReconciliationConfig = ReconciliationConfig()
    strategy: StrategyConfig = StrategyConfig()
    mcp: McpConfig = McpConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    compliance: ComplianceConfig = ComplianceConfig()

    @model_validator(mode="after")
    def _consistency(self) -> AppConfig:
        if self.edge.target_order_usd > self.risk.max_order_size_usd:
            raise ValueError("edge.target_order_usd must not exceed risk.max_order_size_usd")
        if self.fair_value.twap_lookback_s <= 0:
            raise ValueError("twap_lookback_s must be positive")
        if self.exits.min_exit_price >= self.risk.max_entry_price:
            raise ValueError("exits.min_exit_price must be below risk.max_entry_price")
        return self
