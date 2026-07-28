from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from trading.domain.enums import (
    ComparisonOperator,
    ConditionType,
    EventDirection,
    ExposureKind,
    ForecastStatus,
    Horizon,
    MarketDataSourceKind,
    MarketTradeEventKind,
    OrderSide,
    OrdinalBucket,
    PolicyAction,
    PolicyTargetKind,
)
from trading.domain.time import require_aware_utc


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRecord(DomainModel):
    source_id: str
    provider: str
    external_id: str
    revision: int = Field(ge=0)
    content_type: str
    event_time: datetime | None
    published_at: datetime
    available_at: datetime
    ingested_at: datetime
    revised_at: datetime | None
    content_hash: str
    raw_object_uri: str | None
    license_policy_id: str
    metadata: dict[str, JsonValue]

    @field_validator(
        "event_time",
        "published_at",
        "available_at",
        "ingested_at",
        "revised_at",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware_utc(value)


class MarketBar(DomainModel):
    bar_id: str
    provider: str
    feed: str
    symbol: str
    timeframe: str
    event_time: datetime
    provider_timestamp: str
    available_at: datetime
    ingested_at: datetime
    source_kind: MarketDataSourceKind
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    vwap: Decimal | None = Field(default=None, ge=0)
    trade_count: int = Field(ge=0)
    request_id: str | None
    payload_hash: str
    raw_object_uri: str | None
    payload: dict[str, JsonValue]

    @field_validator("event_time", "available_at", "ingested_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_ohlc(self) -> Self:
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("MarketBar high must cover open, low, and close")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("MarketBar low must cover open, high, and close")
        if self.ingested_at < self.available_at:
            raise ValueError("MarketBar ingested_at cannot precede available_at")
        return self


class MarketQuote(DomainModel):
    quote_id: str
    provider: str
    feed: str
    symbol: str
    event_time: datetime
    provider_timestamp: str
    available_at: datetime
    ingested_at: datetime
    source_kind: MarketDataSourceKind
    bid_exchange: str | None
    bid_price: Decimal = Field(ge=0)
    bid_size_round_lots: int = Field(ge=0)
    ask_exchange: str | None
    ask_price: Decimal = Field(ge=0)
    ask_size_round_lots: int = Field(ge=0)
    conditions: list[str]
    tape: str | None
    payload_hash: str
    raw_object_uri: str | None
    payload: dict[str, JsonValue]

    @field_validator("event_time", "available_at", "ingested_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.ingested_at < self.available_at:
            raise ValueError("MarketQuote ingested_at cannot precede available_at")
        return self


class MarketTradeEvent(DomainModel):
    trade_event_id: str
    provider: str
    feed: str
    symbol: str
    event_kind: MarketTradeEventKind
    provider_event_id: str | None
    event_time: datetime
    provider_timestamp: str
    available_at: datetime
    ingested_at: datetime
    source_kind: MarketDataSourceKind
    exchange: str | None
    price: Decimal | None = Field(default=None, ge=0)
    size: Decimal | None = Field(default=None, ge=0)
    conditions: list[str]
    tape: str | None
    payload_hash: str
    raw_object_uri: str | None
    payload: dict[str, JsonValue]

    @field_validator("event_time", "available_at", "ingested_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.ingested_at < self.available_at:
            raise ValueError("MarketTradeEvent ingested_at cannot precede available_at")
        if self.event_kind is MarketTradeEventKind.TRADE and (
            self.price is None or self.price <= 0 or self.size is None or self.size <= 0
        ):
            raise ValueError("TRADE events require positive price and size")
        return self


class FeatureValue(DomainModel):
    name: str
    value: float
    unit: str
    source_record_ids: list[str]
    feature_code_version: str


class FeatureSnapshot(DomainModel):
    feature_snapshot_id: str
    symbol: str | None
    decision_time: datetime
    data_available_cutoff: datetime
    feature_set_version: str
    values: list[FeatureValue]
    input_manifest_hash: str
    created_at: datetime

    @field_validator("decision_time", "data_available_cutoff", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_cutoff(self) -> Self:
        if self.data_available_cutoff > self.decision_time:
            raise ValueError("Feature data_available_cutoff must not exceed decision_time")
        return self


class StrategyForecast(DomainModel):
    forecast_id: str
    hypothesis_id: str
    strategy_id: str
    strategy_version: str
    experiment_version: str
    decision_time: datetime
    data_available_cutoff: datetime
    horizon: Horizon
    expires_at: datetime
    reference_portfolio_id: str
    exposure_kind: ExposureKind
    unit_exposure: dict[str, float]
    risk_unit_horizon_vol: float = Field(gt=0)
    raw_signal: float
    raw_signal_definition_version: str
    expected_gross_return_bps: float
    standalone_expected_cost_bps: float = Field(ge=0)
    expected_net_return_bps: float
    forecast_error_sd_bps: float = Field(gt=0)
    probability_net_positive: float = Field(ge=0, le=1)
    quantile_10_bps: float
    quantile_50_bps: float
    quantile_90_bps: float
    effective_sample_size: float = Field(ge=0)
    calibration_shrinkage: float = Field(ge=0, le=1)
    health_multiplier: float = Field(ge=0, le=1)
    max_risk_units: float = Field(ge=0)
    capacity_usd: Decimal = Field(ge=0)
    feature_snapshot_ids: list[str]
    calibration_version: str
    code_commit: str
    status: ForecastStatus
    created_at: datetime

    @field_validator(
        "decision_time",
        "data_available_cutoff",
        "expires_at",
        "created_at",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_forecast(self) -> Self:
        if self.data_available_cutoff > self.decision_time:
            raise ValueError("Forecast uses data that was unavailable at decision_time")
        if self.expires_at <= self.decision_time:
            raise ValueError("Forecast expires_at must be after decision_time")
        if abs(sum(self.unit_exposure.values())) > 1e-8:
            raise ValueError("unit_exposure must sum to zero including USD_CASH")
        expected = self.expected_gross_return_bps - self.standalone_expected_cost_bps
        if abs(expected - self.expected_net_return_bps) > 1e-8:
            raise ValueError("expected_net_return_bps arithmetic mismatch")
        if self.status is ForecastStatus.NO_SIGNAL and self.max_risk_units != 0:
            raise ValueError("NO_SIGNAL forecast must have max_risk_units=0")
        if not self.quantile_10_bps <= self.quantile_50_bps <= self.quantile_90_bps:
            raise ValueError("Forecast quantiles must be monotonic")
        return self


class TypedCondition(DomainModel):
    condition_id: str
    condition_type: ConditionType
    field: str
    operator: ComparisonOperator
    value: str | int | float | bool | None
    evaluation_window: str | None
    source_ids: list[str]


class NewsFact(DomainModel):
    statement: str
    source_id: str
    certainty: float = Field(ge=0, le=1)
    is_official_source: bool


class AssetImpactAssessment(DomainModel):
    symbol_or_factor: str
    direction: EventDirection
    severity_bucket: OrdinalBucket
    horizon: Horizon
    transmission_channels: list[str]
    raw_confidence: float = Field(ge=0, le=1)


class NewsEvent(DomainModel):
    news_event_id: str
    schema_version: str
    model_run_id: str
    as_of: datetime
    data_available_cutoff: datetime
    source_event_ids: list[str]
    event_type: str
    actors: list[str]
    facts: list[NewsFact]
    impacts: list[AssetImpactAssessment]
    novelty_bucket: OrdinalBucket
    contradiction_source_ids: list[str]
    invalidation_conditions: list[TypedCondition]
    expires_at: datetime
    prompt_hash: str
    context_manifest_hash: str
    output_hash: str
    created_at: datetime

    @field_validator(
        "as_of", "data_available_cutoff", "expires_at", "created_at", mode="after"
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.data_available_cutoff > self.as_of:
            raise ValueError("NewsEvent uses data unavailable at as_of")
        if self.expires_at <= self.as_of:
            raise ValueError("NewsEvent expires_at must be after as_of")
        if not self.source_event_ids:
            raise ValueError("NewsEvent requires source evidence")
        return self


class PolicyOperation(DomainModel):
    action: PolicyAction
    target_kind: PolicyTargetKind
    target_id: str
    risk_budget_delta: float | None
    risk_multiplier: float | None
    blocked: bool | None


class PolicyPatch(DomainModel):
    patch_id: str
    schema_version: str
    arm_scope: str
    base_policy_version: int = Field(ge=0)
    effective_from: datetime
    expires_at: datetime
    operations: list[PolicyOperation] = Field(min_length=1)
    evidence_news_event_ids: list[str] = Field(min_length=1)
    raw_confidence: float = Field(ge=0, le=1)
    rollback_conditions: list[TypedCondition]
    model_run_id: str
    prompt_hash: str
    context_manifest_hash: str
    created_at: datetime

    @field_validator("effective_from", "expires_at", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.expires_at <= self.effective_from:
            raise ValueError("PolicyPatch expires_at must be after effective_from")
        return self


class PortfolioDecision(DomainModel):
    portfolio_decision_id: str
    arm_id: str
    decision_time: datetime
    core_portfolio_version: str
    policy_version: int
    forecast_ids: list[str]
    input_snapshot_hash: str
    previous_weights: dict[str, float]
    target_weights_pre_risk: dict[str, float]
    expected_net_return_bps: float
    expected_annualized_vol: float = Field(ge=0)
    expected_cvar_975: float
    expected_turnover: float = Field(ge=0)
    expected_cost_usd: Decimal = Field(ge=0)
    optimizer_status: str
    solver_name: str
    solver_diagnostics: dict[str, JsonValue]
    created_at: datetime

    @field_validator("decision_time", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)


class RiskDecision(DomainModel):
    risk_decision_id: str
    portfolio_decision_id: str
    approved: bool
    approved_target_weights: dict[str, float]
    rejected_reasons: list[str]
    forced_reduction_actions: list[dict[str, JsonValue]]
    risk_config_version: str
    market_data_age_seconds: float = Field(ge=0)
    created_at: datetime

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_weights(self) -> Self:
        if self.approved:
            if any(weight < -1e-12 for weight in self.approved_target_weights.values()):
                raise ValueError("Phase 0 approved weights must be long-only")
            if abs(sum(self.approved_target_weights.values()) - 1.0) > 1e-8:
                raise ValueError("Approved weights must sum to one including USD_CASH")
        return self


class OrderIntent(DomainModel):
    order_intent_id: str
    arm_id: str
    portfolio_decision_id: str
    risk_decision_id: str
    symbol: str
    side: OrderSide
    order_type: str
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal | None
    time_in_force: str
    session: str
    client_order_id: str
    idempotency_key: str
    created_at: datetime

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)


class Fill(DomainModel):
    fill_id: str
    order_intent_id: str
    arm_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    commission_usd: Decimal = Field(ge=0)
    execution_scenario_id: str
    effective_at: datetime
    created_at: datetime

    @field_validator("effective_at", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)


class LedgerTransaction(DomainModel):
    ledger_transaction_id: str
    arm_id: str
    transaction_type: str
    source_id: str
    effective_at: datetime
    created_at: datetime

    @field_validator("effective_at", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)


class LedgerPosting(DomainModel):
    posting_id: str
    ledger_transaction_id: str
    account_code: str
    asset_code: str
    quantity_delta: Decimal
    usd_value_delta: Decimal
    metadata: dict[str, JsonValue]


class LedgerEntry(DomainModel):
    transaction: LedgerTransaction
    postings: list[LedgerPosting] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_balance(self) -> Self:
        if any(
            posting.ledger_transaction_id != self.transaction.ledger_transaction_id
            for posting in self.postings
        ):
            raise ValueError("Ledger posting references another transaction")
        if abs(sum((posting.usd_value_delta for posting in self.postings), Decimal("0"))) > Decimal(
            "0.000001"
        ):
            raise ValueError("Ledger transaction is not balanced")
        return self


class NavSnapshot(DomainModel):
    nav_snapshot_id: str
    arm_id: str
    as_of: datetime
    cash_usd: Decimal
    positions_market_value_usd: Decimal
    nav_usd: Decimal
    price_manifest_hash: str
    created_at: datetime

    @field_validator("as_of", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_nav(self) -> Self:
        if self.cash_usd + self.positions_market_value_usd != self.nav_usd:
            raise ValueError("NAV arithmetic mismatch")
        return self


def model_payload(model: DomainModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
