from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, JsonValue, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.time import require_aware_utc

Q1_ALGORITHM_VERSION = "q1_math_core_v1"


class Q1ArmId(StrEnum):
    HOLD = "HOLD"
    LIVE_MIRROR = "LIVE-MIRROR"
    B0_CASH = "B0-CASH"
    B0_QQQ = "B0-QQQ"
    B0_VOL = "B0-VOL"
    Q1_DET = "Q1-DET"
    Q1_LLM = "Q1-LLM"


class OrderEventType(StrEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED_BY_RISK = "CANCELED_BY_RISK"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    BLOCKED_BY_DATA = "BLOCKED_BY_DATA"
    BLOCKED_BY_PRICE_GUARD = "BLOCKED_BY_PRICE_GUARD"


TERMINAL_ORDER_EVENT_TYPES = frozenset(
    {
        OrderEventType.FILLED,
        OrderEventType.CANCELED_BY_RISK,
        OrderEventType.SUPERSEDED,
        OrderEventType.EXPIRED,
        OrderEventType.REJECTED,
    }
)


def is_terminal_order_event(event_type: OrderEventType) -> bool:
    return event_type in TERMINAL_ORDER_EVENT_TYPES


class RiskSeverity(StrEnum):
    NORMAL = "NORMAL"
    SOFT_STOP = "SOFT_STOP"
    HARD_REDUCE = "HARD_REDUCE"
    CRITICAL_EXIT = "CRITICAL_EXIT"


class RiskEpisodeEventType(StrEnum):
    ACTIVATE = "ACTIVATE"
    ESCALATE = "ESCALATE"
    TARGET_PROGRESS = "TARGET_PROGRESS"
    TARGET_REACHED = "TARGET_REACHED"
    RELEASE = "RELEASE"


class CashSettlementEventType(StrEnum):
    OPENING_SETTLED_CASH = "OPENING_SETTLED_CASH"
    BUY_SETTLED_CASH_DEBIT = "BUY_SETTLED_CASH_DEBIT"
    SELL_RECEIVABLE_CREATED = "SELL_RECEIVABLE_CREATED"
    RECEIVABLE_SETTLED = "RECEIVABLE_SETTLED"


class MatchedComparison(StrEnum):
    Q1_DET_MINUS_B0_VOL = "Q1_DET_MINUS_B0_VOL"
    Q1_LLM_MINUS_Q1_DET = "Q1_LLM_MINUS_Q1_DET"


class VersionedQ1Record(DomainModel):
    algorithm_version: str = Field(default=Q1_ALGORITHM_VERSION, pattern=r"^q1_math_core_v1$")
    config_manifest_hash: str = Field(min_length=1, max_length=64)
    code_version: str = Field(min_length=1, max_length=80)
    model_version: str = Field(min_length=1, max_length=120)
    source_manifest_hash: str = Field(min_length=1, max_length=64)


class MarketCalendarSession(VersionedQ1Record):
    calendar_session_id: str
    calendar_version: str
    session_date: date
    open_at: datetime
    close_at: datetime
    source: str
    available_at: datetime
    session_hash: str
    created_at: datetime

    @field_validator("open_at", "close_at", "available_at", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_session(self) -> Self:
        if self.close_at <= self.open_at:
            raise ValueError("Market calendar close_at must be after open_at")
        if self.created_at < self.available_at:
            raise ValueError("Market calendar row cannot be created before available_at")
        return self


class StrategyEvaluationAnchor(VersionedQ1Record):
    evaluation_anchor_id: str
    run_id: str
    calendar_session_id: str
    common_t0_at: datetime
    initial_nav_usd: Decimal = Field(gt=0)
    quote_manifest_hash: str
    anchor_hash: str
    created_at: datetime

    @field_validator("common_t0_at", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_created_at(self) -> Self:
        if self.created_at < self.common_t0_at:
            raise ValueError("Evaluation anchor cannot be created before common T0")
        return self


class PointInTimeSourceReference(DomainModel):
    record_id: str
    available_at: datetime

    @field_validator("available_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)


class Q1DecisionInputManifest(VersionedQ1Record):
    calendar_session_id: str
    source_bars: tuple[PointInTimeSourceReference, ...]
    quotes: tuple[PointInTimeSourceReference, ...]
    manifest_hash: str

    @model_validator(mode="after")
    def validate_unique_sources(self) -> Self:
        bar_ids = [item.record_id for item in self.source_bars]
        quote_ids = [item.record_id for item in self.quotes]
        if len(bar_ids) != len(set(bar_ids)):
            raise ValueError("Decision source bar IDs must be unique")
        if len(quote_ids) != len(set(quote_ids)):
            raise ValueError("Decision quote IDs must be unique")
        return self


class Q1StrategyDecision(VersionedQ1Record):
    portfolio_decision_id: str
    run_id: str
    arm_id: Q1ArmId
    source_cycle_id: str
    input_state_sequence: int = Field(ge=0)
    decision_kind: str
    scheduled_at: datetime
    signal_data_cutoff: datetime
    portfolio_state_as_of: datetime
    quote_as_of: datetime
    decision_created_at: datetime
    valid_until: datetime
    input_manifest: Q1DecisionInputManifest
    target_weights: dict[str, Decimal]
    diagnostics: dict[str, JsonValue]
    worker_fence_token: str
    cycle_attempt_count: int = Field(ge=1)
    decision_hash: str

    @field_validator(
        "scheduled_at",
        "signal_data_cutoff",
        "portfolio_state_as_of",
        "quote_as_of",
        "decision_created_at",
        "valid_until",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_point_in_time_contract(self) -> Self:
        if self.signal_data_cutoff > self.scheduled_at:
            raise ValueError("signal_data_cutoff cannot exceed scheduled_at")
        if self.portfolio_state_as_of > self.decision_created_at:
            raise ValueError("portfolio_state_as_of cannot exceed decision_created_at")
        if self.quote_as_of > self.decision_created_at:
            raise ValueError("quote_as_of cannot exceed decision_created_at")
        if self.valid_until <= self.decision_created_at:
            raise ValueError("valid_until must be after decision_created_at")
        if self.input_manifest.calendar_session_id == "":
            raise ValueError("Decision input manifest requires a calendar session")
        if (
            self.config_manifest_hash != self.input_manifest.config_manifest_hash
            or self.code_version != self.input_manifest.code_version
            or self.model_version != self.input_manifest.model_version
            or self.source_manifest_hash != self.input_manifest.source_manifest_hash
        ):
            raise ValueError("Decision and input-manifest versions must match")
        if any(
            item.available_at > self.signal_data_cutoff
            for item in self.input_manifest.source_bars
        ):
            raise ValueError("Decision contains a bar unavailable at signal cutoff")
        if any(
            item.available_at > self.quote_as_of
            for item in self.input_manifest.quotes
        ):
            raise ValueError("Decision contains a quote unavailable at quote_as_of")
        if any(weight < 0 for weight in self.target_weights.values()):
            raise ValueError("Q1 target weights must be long-only")
        if abs(sum(self.target_weights.values(), Decimal("0")) - Decimal("1")) > Decimal(
            "0.0000000001"
        ):
            raise ValueError("Q1 target weights must sum to one including USD_CASH")
        return self


class OrderEvent(VersionedQ1Record):
    event_id: str
    order_intent_id: str
    event_type: OrderEventType
    event_sequence: int = Field(ge=1)
    quantity_delta: Decimal = Field(ge=0)
    commission_delta_usd: Decimal = Field(ge=0)
    remaining_quantity: Decimal = Field(ge=0)
    cumulative_filled_quantity: Decimal = Field(ge=0)
    cumulative_commission_usd: Decimal = Field(ge=0)
    occurred_at: datetime
    available_at: datetime
    idempotency_key: str
    reason: str | None = None
    source_id: str | None = None
    quote_id: str | None = None
    source_cycle_id: str | None = None
    worker_fence_token: str
    cycle_attempt_count: int = Field(ge=1)
    event_hash: str

    @field_validator("occurred_at", "available_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.available_at < self.occurred_at:
            raise ValueError("Order event available_at cannot precede occurred_at")
        if self.event_type is OrderEventType.FILLED and self.remaining_quantity != 0:
            raise ValueError("FILLED order event must have zero remaining quantity")
        if (
            self.event_type is OrderEventType.PARTIALLY_FILLED
            and self.cumulative_filled_quantity <= 0
        ):
            raise ValueError("PARTIALLY_FILLED requires a positive cumulative fill")
        if (
            self.event_type
            in {OrderEventType.PARTIALLY_FILLED, OrderEventType.FILLED}
            and self.quantity_delta <= 0
        ):
            raise ValueError("Fill events require a positive quantity_delta")
        if (
            self.event_type
            not in {OrderEventType.PARTIALLY_FILLED, OrderEventType.FILLED}
            and (
                self.quantity_delta != 0
                or self.commission_delta_usd != 0
            )
        ):
            raise ValueError("Non-fill order events cannot change quantity or commission")
        return self


class RiskTarget(DomainModel):
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9._-]{0,29}$")
    target_quantity: Decimal = Field(ge=0)
    trigger_quote_id: str
    target_generation: int = Field(default=1, ge=1)
    target_id: str | None = None
    trigger_quantity: Decimal | None = Field(default=None, ge=0)
    trigger_price: Decimal | None = Field(default=None, gt=0)
    target_weight: Decimal | None = Field(default=None, ge=0, le=1)


class RiskEpisode(VersionedQ1Record):
    risk_episode_id: str
    run_id: str
    arm_id: Q1ArmId
    severity: RiskSeverity
    calendar_session_id: str
    triggered_at: datetime
    trigger_nav_usd: Decimal = Field(gt=0)
    session_open_nav_usd: Decimal = Field(gt=0)
    running_peak_nav_usd: Decimal = Field(gt=0)
    daily_loss: Decimal = Field(ge=0)
    run_drawdown: Decimal = Field(ge=0)
    portfolio_annualized_vol: Decimal | None = Field(default=None, ge=0)
    soft_daily_threshold: Decimal = Field(ge=0)
    hard_daily_threshold: Decimal = Field(ge=0)
    reconciliation_status: str
    targets: tuple[RiskTarget, ...] = Field(min_length=1)
    target_manifest_hash: str
    episode_hash: str
    created_at: datetime

    @field_validator("triggered_at", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_episode(self) -> Self:
        if self.severity not in {RiskSeverity.HARD_REDUCE, RiskSeverity.CRITICAL_EXIT}:
            raise ValueError("Typed risk episodes require HARD_REDUCE or CRITICAL_EXIT")
        if len({target.symbol for target in self.targets}) != len(self.targets):
            raise ValueError("Risk episode targets must have unique symbols")
        if any(target.target_generation != 1 for target in self.targets):
            raise ValueError("Risk episode activation targets must use generation 1")
        if self.created_at < self.triggered_at:
            raise ValueError("Risk episode cannot be created before it is triggered")
        return self


class RiskEpisodeEvent(VersionedQ1Record):
    risk_episode_event_id: str
    risk_episode_id: str
    event_type: RiskEpisodeEventType
    event_sequence: int = Field(ge=1)
    severity: RiskSeverity
    target_generation: int = Field(default=1, ge=1)
    occurred_at: datetime
    available_at: datetime
    targets: tuple[RiskTarget, ...] = ()
    target_symbol: str | None = None
    observed_quantity: Decimal | None = Field(default=None, ge=0)
    residual_quantity: Decimal | None = Field(default=None, ge=0)
    consecutive_valid_checks: int = Field(default=0, ge=0)
    source_cycle_id: str | None = None
    worker_fence_token: str
    cycle_attempt_count: int = Field(ge=1)
    idempotency_key: str
    event_hash: str

    @field_validator("occurred_at", "available_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.available_at < self.occurred_at:
            raise ValueError("Risk event available_at cannot precede occurred_at")
        if self.event_type in {
            RiskEpisodeEventType.ACTIVATE,
            RiskEpisodeEventType.ESCALATE,
        } and not self.targets:
            raise ValueError("ACTIVATE and ESCALATE events require non-empty typed targets")
        if self.event_type in {
            RiskEpisodeEventType.TARGET_PROGRESS,
            RiskEpisodeEventType.TARGET_REACHED,
        } and self.target_symbol is None:
            raise ValueError("Target progress events require target_symbol")
        return self


class CashSettlementEvent(VersionedQ1Record):
    cash_settlement_event_id: str
    run_id: str
    arm_id: Q1ArmId
    event_type: CashSettlementEventType
    receivable_id: str | None
    source_fill_id: str | None
    settlement_policy_version: str
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    settled_cash_delta_usd: Decimal
    unsettled_receivable_delta_usd: Decimal
    gross_amount_usd: Decimal = Field(ge=0)
    commission_usd: Decimal = Field(ge=0)
    trade_at: datetime | None
    settlement_date: date | None
    effective_at: datetime
    calendar_session_id: str
    source_cycle_id: str
    worker_fence_token: str
    cycle_attempt_count: int = Field(ge=1)
    idempotency_key: str
    event_hash: str
    created_at: datetime

    @field_validator("trade_at", "effective_at", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware_utc(value)

    @model_validator(mode="after")
    def validate_settlement(self) -> Self:
        receivable_events = {
            CashSettlementEventType.SELL_RECEIVABLE_CREATED,
            CashSettlementEventType.RECEIVABLE_SETTLED,
        }
        if self.event_type in receivable_events and (
            self.receivable_id is None or self.settlement_date is None
        ):
            raise ValueError("Receivable events require receivable_id and settlement_date")
        if self.created_at < self.effective_at:
            raise ValueError("Settlement event cannot be created before effective_at")
        return self


class StrategyDailyResult(VersionedQ1Record):
    strategy_daily_result_id: str
    evaluation_anchor_id: str
    run_id: str
    arm_id: Q1ArmId
    calendar_session_id: str
    session_date: date
    valuation_at: datetime
    nav_usd: Decimal = Field(gt=0)
    net_daily_return: Decimal
    cumulative_return: Decimal
    daily_turnover: Decimal = Field(ge=0)
    cumulative_turnover: Decimal = Field(ge=0)
    commissions_usd: Decimal = Field(ge=0)
    spread_cost_usd: Decimal = Field(ge=0)
    delay_cost_usd: Decimal = Field(ge=0)
    sensitivity_5bp_usd: Decimal = Field(ge=0)
    sensitivity_10bp_usd: Decimal = Field(ge=0)
    cash_weight: Decimal = Field(ge=0, le=1)
    qqq_weight: Decimal = Field(ge=0, le=1)
    soxx_weight: Decimal = Field(ge=0, le=1)
    active_risk_episode_count: int = Field(ge=0)
    active_llm_reduction_count: int = Field(ge=0)
    result_hash: str
    created_at: datetime

    @field_validator("valuation_at", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_weights(self) -> Self:
        if self.cash_weight + self.qqq_weight + self.soxx_weight > Decimal("1.0000000001"):
            raise ValueError("Daily result weights cannot imply leverage")
        if self.created_at < self.valuation_at:
            raise ValueError("Daily result cannot be created before valuation_at")
        return self


class MatchedAttributionResult(VersionedQ1Record):
    matched_attribution_result_id: str
    evaluation_anchor_id: str
    run_id: str
    comparison: MatchedComparison
    left_arm_id: Q1ArmId
    right_arm_id: Q1ArmId
    through_session_date: date
    common_valid_sessions: int = Field(ge=0)
    mean_daily_difference: Decimal
    annualized_difference: Decimal
    newey_west_lag: int = Field(ge=0)
    newey_west_standard_error: Decimal = Field(ge=0)
    bootstrap_seed: int
    bootstrap_lower: Decimal
    bootstrap_upper: Decimal
    promotion_ready: bool
    result_hash: str
    created_at: datetime

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        expected = {
            MatchedComparison.Q1_DET_MINUS_B0_VOL: (
                Q1ArmId.Q1_DET,
                Q1ArmId.B0_VOL,
            ),
            MatchedComparison.Q1_LLM_MINUS_Q1_DET: (
                Q1ArmId.Q1_LLM,
                Q1ArmId.Q1_DET,
            ),
        }[self.comparison]
        if (self.left_arm_id, self.right_arm_id) != expected:
            raise ValueError("Matched attribution arm pair does not match comparison")
        if self.bootstrap_lower > self.bootstrap_upper:
            raise ValueError("Bootstrap interval must be ordered")
        if self.promotion_ready and self.common_valid_sessions < 126:
            raise ValueError("Promotion readiness requires at least 126 common sessions")
        return self
