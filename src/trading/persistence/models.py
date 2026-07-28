from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    mode: Mapped[str] = mapped_column(String(20))
    experiment_version: Mapped[str] = mapped_column(String(80))
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    code_commit: Mapped[str] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30))
    result_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result_hash: Mapped[str | None] = mapped_column(String(64))


class PaperAccountSpecRow(Base):
    __tablename__ = "paper_account_specs"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "version",
            name="uq_paper_account_spec_version",
        ),
    )

    account_spec_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[str] = mapped_column(String(40))
    base_currency: Mapped[str] = mapped_column(String(3))
    source: Mapped[str] = mapped_column(String(40))
    config_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class PaperCashBalanceRow(Base):
    __tablename__ = "paper_cash_balances"
    __table_args__ = (
        UniqueConstraint(
            "account_spec_id",
            "currency",
            name="uq_paper_cash_spec_currency",
        ),
    )

    cash_balance_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    account_spec_id: Mapped[str] = mapped_column(
        ForeignKey("paper_account_specs.account_spec_id", ondelete="RESTRICT")
    )
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    tradable: Mapped[bool] = mapped_column(Boolean)
    exclusion_reason: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class PaperPositionRow(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (
        UniqueConstraint(
            "account_spec_id",
            "symbol",
            name="uq_paper_position_spec_symbol",
        ),
    )

    paper_position_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    account_spec_id: Mapped[str] = mapped_column(
        ForeignKey("paper_account_specs.account_spec_id", ondelete="RESTRICT")
    )
    symbol: Mapped[str] = mapped_column(String(30))
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class PaperBootstrapMarkRow(Base):
    __tablename__ = "paper_bootstrap_marks"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "symbol",
            name="uq_paper_bootstrap_run_symbol",
        ),
    )

    bootstrap_mark_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    account_spec_id: Mapped[str] = mapped_column(
        ForeignKey("paper_account_specs.account_spec_id", ondelete="RESTRICT")
    )
    symbol: Mapped[str] = mapped_column(String(30))
    price: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    currency: Mapped[str] = mapped_column(String(3))
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_kind: Mapped[str] = mapped_column(String(40))
    source_record_id: Mapped[str | None] = mapped_column(String(100))
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class PaperBootstrapCompletionRow(Base):
    __tablename__ = "paper_bootstrap_completions"

    bootstrap_completion_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"), unique=True)
    account_spec_id: Mapped[str] = mapped_column(
        ForeignKey("paper_account_specs.account_spec_id", ondelete="RESTRICT")
    )
    common_mark_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    initial_nav_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    input_manifest_hash: Mapped[str] = mapped_column(String(64))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class PaperCycleRow(Base):
    __tablename__ = "paper_cycles"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "cycle_kind",
            "scheduled_at",
            name="uq_paper_cycle_schedule",
        ),
    )

    cycle_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    cycle_kind: Mapped[str] = mapped_column(String(40))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_available_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30))
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    input_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    output_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    last_error_detail: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperRuntimeStatusRow(Base):
    __tablename__ = "paper_runtime_status"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id", ondelete="RESTRICT"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(30))
    current_cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_cycles.cycle_id", ondelete="SET NULL")
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_cycle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    last_error_detail: Mapped[str | None] = mapped_column(String(500))
    process_id: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DomainEventRow(Base):
    __tablename__ = "domain_events"

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(80))
    aggregate_id: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(100))
    event_version: Mapped[str] = mapped_column(String(20))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    causation_id: Mapped[str | None] = mapped_column(String(80))
    correlation_id: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutboxEventRow(Base):
    __tablename__ = "outbox_events"

    outbox_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("domain_events.event_id", ondelete="RESTRICT"), unique=True
    )
    topic: Mapped[str] = mapped_column(String(100))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProcessedEventRow(Base):
    __tablename__ = "processed_events"

    consumer_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("domain_events.event_id", ondelete="RESTRICT"), primary_key=True
    )
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result_hash: Mapped[str] = mapped_column(String(64))


class SourceRecordRow(Base):
    __tablename__ = "source_records"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", "revision", name="uq_source_revision"),
    )

    source_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    provider: Mapped[str] = mapped_column(String(80))
    external_id: Mapped[str] = mapped_column(String(120))
    revision: Mapped[int] = mapped_column(Integer)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class MarketBarRow(Base):
    __tablename__ = "market_bars"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "feed",
            "symbol",
            "timeframe",
            "provider_timestamp",
            "payload_hash",
            name="uq_market_bar_payload",
        ),
    )

    bar_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    feed: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(30))
    timeframe: Mapped[str] = mapped_column(String(20))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider_timestamp: Mapped[str] = mapped_column(String(50))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_kind: Mapped[str] = mapped_column(String(30))
    open: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    high: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    low: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    close: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    volume: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    vwap: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    trade_count: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String(100))
    payload_hash: Mapped[str] = mapped_column(String(64))
    raw_object_uri: Mapped[str | None] = mapped_column(String(500))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class MarketQuoteRow(Base):
    __tablename__ = "market_quotes"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "feed",
            "symbol",
            "provider_timestamp",
            "payload_hash",
            name="uq_market_quote_payload",
        ),
    )

    quote_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    feed: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(30))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider_timestamp: Mapped[str] = mapped_column(String(50))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_kind: Mapped[str] = mapped_column(String(30))
    bid_exchange: Mapped[str | None] = mapped_column(String(20))
    bid_price: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    bid_size_round_lots: Mapped[int] = mapped_column(Integer)
    ask_exchange: Mapped[str | None] = mapped_column(String(20))
    ask_price: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    ask_size_round_lots: Mapped[int] = mapped_column(Integer)
    conditions: Mapped[list[str]] = mapped_column(JSON)
    tape: Mapped[str | None] = mapped_column(String(10))
    payload_hash: Mapped[str] = mapped_column(String(64))
    raw_object_uri: Mapped[str | None] = mapped_column(String(500))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class MarketTradeEventRow(Base):
    __tablename__ = "market_trade_events"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "feed",
            "symbol",
            "event_kind",
            "provider_timestamp",
            "payload_hash",
            name="uq_market_trade_event_payload",
        ),
    )

    trade_event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    feed: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(30))
    event_kind: Mapped[str] = mapped_column(String(30))
    provider_event_id: Mapped[str | None] = mapped_column(String(100))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider_timestamp: Mapped[str] = mapped_column(String(50))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_kind: Mapped[str] = mapped_column(String(30))
    exchange: Mapped[str | None] = mapped_column(String(20))
    price: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    size: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    conditions: Mapped[list[str]] = mapped_column(JSON)
    tape: Mapped[str | None] = mapped_column(String(10))
    payload_hash: Mapped[str] = mapped_column(String(64))
    raw_object_uri: Mapped[str | None] = mapped_column(String(500))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class MarketStreamStatusRow(Base):
    __tablename__ = "market_stream_status"

    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    feed: Mapped[str] = mapped_column(String(20), primary_key=True)
    state: Mapped[str] = mapped_column(String(30))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_bar_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconnect_count: Mapped[int] = mapped_column(Integer)
    consecutive_failures: Mapped[int] = mapped_column(Integer)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    last_error_detail: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FeatureSnapshotRow(Base):
    __tablename__ = "feature_snapshots"

    feature_snapshot_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(30))
    decision_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_available_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    input_manifest_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class StrategyForecastRow(Base):
    __tablename__ = "strategy_forecasts"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id",
            "strategy_version",
            "experiment_version",
            "decision_time",
            "horizon",
            "input_manifest_hash",
            name="uq_strategy_forecast_identity",
        ),
    )

    forecast_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(40))
    strategy_version: Mapped[str] = mapped_column(String(40))
    experiment_version: Mapped[str] = mapped_column(String(80))
    decision_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    horizon: Mapped[str] = mapped_column(String(10))
    input_manifest_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class NewsEventRow(Base):
    __tablename__ = "news_events"

    news_event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_hash: Mapped[str] = mapped_column(String(64))


class PolicyPatchRow(Base):
    __tablename__ = "policy_patches"

    patch_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(80), default="legacy_global")
    arm_scope: Mapped[str] = mapped_column(String(30))
    base_policy_version: Mapped[int] = mapped_column(Integer)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class PolicyVersionRow(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint(
            "scope_id",
            "arm_id",
            "version",
            name="uq_scope_arm_policy_version",
        ),
    )

    policy_version_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(80), default="legacy_global")
    arm_id: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer)
    source_patch_id: Mapped[str | None] = mapped_column(String(80))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CommanderSelectionRow(Base):
    __tablename__ = "commander_selections"

    selection_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    reasoning_profile: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    config_hash: Mapped[str] = mapped_column(String(64))


class CommanderRequestRow(Base):
    __tablename__ = "commander_requests"

    request_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(80), default="legacy_global")
    selection_id: Mapped[str] = mapped_column(
        ForeignKey("commander_selections.selection_id", ondelete="RESTRICT")
    )
    selection_version: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(40))
    arm_scope: Mapped[str] = mapped_column(String(30))
    base_policy_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    context_manifest_hash: Mapped[str] = mapped_column(String(64))
    prompt_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class CommanderDecisionRow(Base):
    __tablename__ = "commander_decisions"
    __table_args__ = (UniqueConstraint("request_id", name="uq_commander_decision_request"),)

    decision_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("commander_requests.request_id", ondelete="RESTRICT")
    )
    provider: Mapped[str] = mapped_column(String(40))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class CommanderDecisionResultRow(Base):
    __tablename__ = "commander_decision_results"

    result_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    decision_id: Mapped[str] = mapped_column(
        ForeignKey("commander_decisions.decision_id", ondelete="RESTRICT"), unique=True
    )
    status: Mapped[str] = mapped_column(String(20))
    reason_code: Mapped[str] = mapped_column(String(80))
    reason_detail: Mapped[str] = mapped_column(String(500))
    arm_scope: Mapped[str] = mapped_column(String(30))
    base_policy_version: Mapped[int] = mapped_column(Integer)
    applied_policy_version: Mapped[int | None] = mapped_column(Integer)
    compiled_policy_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class ShadowArmRow(Base):
    __tablename__ = "shadow_arms"
    __table_args__ = (UniqueConstraint("run_id", "arm_id", name="uq_run_arm"),)

    arm_instance_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    arm_id: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ArmStateSnapshotRow(Base):
    __tablename__ = "arm_state_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", "arm_id", "sequence", name="uq_arm_state_sequence"),
    )

    arm_state_snapshot_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    arm_id: Mapped[str] = mapped_column(String(30))
    sequence: Mapped[int] = mapped_column(Integer)
    source_cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT")
    )
    state_hash: Mapped[str | None] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioDecisionRow(Base):
    __tablename__ = "portfolio_decisions"

    portfolio_decision_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    arm_id: Mapped[str] = mapped_column(String(30))
    source_cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT")
    )
    input_state_sequence: Mapped[int | None] = mapped_column(Integer)
    decision_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    algorithm_version: Mapped[str | None] = mapped_column(String(80))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signal_data_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    portfolio_state_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quote_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    calendar_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("market_calendar_sessions.calendar_session_id", ondelete="RESTRICT")
    )
    config_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    code_version: Mapped[str | None] = mapped_column(String(80))
    model_version: Mapped[str | None] = mapped_column(String(120))
    source_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    input_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    decision_hash: Mapped[str] = mapped_column(String(64))


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"

    risk_decision_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    portfolio_decision_id: Mapped[str] = mapped_column(
        ForeignKey("portfolio_decisions.portfolio_decision_id")
    )
    source_cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT")
    )
    input_state_sequence: Mapped[int | None] = mapped_column(Integer)
    approved: Mapped[bool] = mapped_column(Boolean)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class OrderIntentRow(Base):
    __tablename__ = "order_intents"

    order_intent_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    arm_id: Mapped[str] = mapped_column(String(30))
    source_cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT")
    )
    input_state_sequence: Mapped[int | None] = mapped_column(Integer)
    symbol: Mapped[str | None] = mapped_column(String(30))
    side: Mapped[str | None] = mapped_column(String(10))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_quote_id: Mapped[str | None] = mapped_column(String(100))
    decision_reference_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    algorithm_version: Mapped[str | None] = mapped_column(String(80))
    config_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    code_version: Mapped[str | None] = mapped_column(String(80))
    model_version: Mapped[str | None] = mapped_column(String(120))
    source_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    decision_spread_bps: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    intent_hash: Mapped[str] = mapped_column(String(64))


class FillRow(Base):
    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint(
            "order_intent_id",
            "quote_id",
            "execution_scenario_id",
            name="uq_fill_order_quote_scenario",
        ),
    )

    fill_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    order_intent_id: Mapped[str] = mapped_column(ForeignKey("order_intents.order_intent_id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    arm_id: Mapped[str] = mapped_column(String(30))
    source_cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT")
    )
    quote_id: Mapped[str | None] = mapped_column(String(100))
    quote_event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quote_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str | None] = mapped_column(String(30))
    side: Mapped[str | None] = mapped_column(String(10))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    price: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    commission_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    execution_scenario_id: Mapped[str | None] = mapped_column(String(80))
    fill_hash: Mapped[str | None] = mapped_column(String(64))
    algorithm_version: Mapped[str | None] = mapped_column(String(80))
    config_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    code_version: Mapped[str | None] = mapped_column(String(80))
    model_version: Mapped[str | None] = mapped_column(String(120))
    source_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    base_fill_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    sensitivity_5bp_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    sensitivity_10bp_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class LedgerTransactionRow(Base):
    __tablename__ = "ledger_transactions"

    ledger_transaction_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    arm_id: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(String(80))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class LedgerPostingRow(Base):
    __tablename__ = "ledger_postings"

    posting_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    ledger_transaction_id: Mapped[str] = mapped_column(
        ForeignKey("ledger_transactions.ledger_transaction_id")
    )
    account_code: Mapped[str] = mapped_column(String(80))
    asset_code: Mapped[str] = mapped_column(String(30))
    quantity_delta: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    usd_value_delta: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class NavSnapshotRow(Base):
    __tablename__ = "nav_snapshots"

    nav_snapshot_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    arm_id: Mapped[str] = mapped_column(String(30))
    source_cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT")
    )
    quote_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    algorithm_version: Mapped[str | None] = mapped_column(String(80))
    config_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    code_version: Mapped[str | None] = mapped_column(String(80))
    model_version: Mapped[str | None] = mapped_column(String(120))
    source_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    nav_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class PaperCycleEffectRow(Base):
    __tablename__ = "paper_cycle_effects"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "effect_kind",
            name="uq_paper_cycle_effect_kind",
        ),
    )

    effect_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    effect_kind: Mapped[str] = mapped_column(String(40))
    input_manifest_hash: Mapped[str] = mapped_column(String(64))
    output_manifest_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperExecutionAttemptRow(Base):
    __tablename__ = "paper_execution_attempts"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "order_intent_id",
            name="uq_execution_attempt_cycle_order",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT"))
    order_intent_id: Mapped[str] = mapped_column(
        ForeignKey("order_intents.order_intent_id", ondelete="RESTRICT")
    )
    quote_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(40))
    remaining_quantity_before: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    remaining_quantity_after: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    cumulative_notional_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    cumulative_commission_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    attempt_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ForwardStrategyCandidateRow(Base):
    __tablename__ = "forward_strategy_candidates"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "strategy_id",
            "strategy_version",
            "decision_time",
            name="uq_forward_candidate_identity",
        ),
    )

    candidate_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    source_cycle_id: Mapped[str] = mapped_column(
        ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT")
    )
    strategy_id: Mapped[str] = mapped_column(String(40))
    strategy_version: Mapped[str] = mapped_column(String(40))
    decision_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_available_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30))
    reason_code: Mapped[str] = mapped_column(String(120))
    input_manifest_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ForecastCalibrationRow(Base):
    __tablename__ = "forecast_calibrations"

    calibration_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(40))
    strategy_version: Mapped[str] = mapped_column(String(40))
    feature_version: Mapped[str] = mapped_column(String(80))
    horizon: Mapped[str] = mapped_column(String(10))
    cost_model_version: Mapped[str] = mapped_column(String(80))
    trained_through: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30))
    artifact_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategyPromotionDecisionRow(Base):
    __tablename__ = "strategy_promotion_decisions"

    promotion_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(40))
    strategy_version: Mapped[str] = mapped_column(String(40))
    calibration_id: Mapped[str | None] = mapped_column(
        ForeignKey("forecast_calibrations.calibration_id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(30))
    reason_code: Mapped[str] = mapped_column(String(120))
    artifact_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketCalendarSessionRow(Base):
    __tablename__ = "market_calendar_sessions"
    __table_args__ = (
        UniqueConstraint(
            "calendar_version",
            "session_date",
            "session_hash",
            name="uq_market_calendar_version_date_hash",
        ),
        CheckConstraint("close_at > open_at", name="ck_market_calendar_positive_session"),
    )

    calendar_session_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    algorithm_version: Mapped[str] = mapped_column(String(80))
    calendar_version: Mapped[str] = mapped_column(String(80))
    session_date: Mapped[date] = mapped_column(Date)
    open_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    close_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(80))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(120))
    source_manifest_hash: Mapped[str] = mapped_column(String(64))
    session_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategyEvaluationAnchorRow(Base):
    __tablename__ = "strategy_evaluation_anchors"

    evaluation_anchor_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"), unique=True)
    algorithm_version: Mapped[str] = mapped_column(String(80))
    calendar_session_id: Mapped[str] = mapped_column(
        ForeignKey("market_calendar_sessions.calendar_session_id", ondelete="RESTRICT")
    )
    common_t0_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    initial_nav_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    quote_manifest_hash: Mapped[str] = mapped_column(String(64))
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(120))
    source_manifest_hash: Mapped[str] = mapped_column(String(64))
    anchor_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RiskEpisodeRow(Base):
    __tablename__ = "risk_episodes"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('HARD_REDUCE', 'CRITICAL_EXIT')",
            name="ck_risk_episode_severity",
        ),
        CheckConstraint("target_count > 0", name="ck_risk_episode_nonempty_targets"),
        UniqueConstraint(
            "run_id",
            "arm_id",
            "episode_hash",
            name="uq_risk_episode_identity",
        ),
    )

    risk_episode_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    arm_id: Mapped[str] = mapped_column(String(30))
    algorithm_version: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(30))
    calendar_session_id: Mapped[str] = mapped_column(
        ForeignKey("market_calendar_sessions.calendar_session_id", ondelete="RESTRICT")
    )
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trigger_nav_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    session_open_nav_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    running_peak_nav_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    daily_loss: Mapped[Decimal] = mapped_column(Numeric(20, 12))
    run_drawdown: Mapped[Decimal] = mapped_column(Numeric(20, 12))
    portfolio_annualized_vol: Mapped[Decimal | None] = mapped_column(Numeric(20, 12))
    soft_daily_threshold: Mapped[Decimal] = mapped_column(Numeric(20, 12))
    hard_daily_threshold: Mapped[Decimal] = mapped_column(Numeric(20, 12))
    reconciliation_status: Mapped[str] = mapped_column(String(40))
    target_manifest_hash: Mapped[str] = mapped_column(String(64))
    target_count: Mapped[int] = mapped_column(Integer)
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(120))
    source_manifest_hash: Mapped[str] = mapped_column(String(64))
    episode_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RiskEpisodeTargetRow(Base):
    __tablename__ = "risk_episode_targets"
    __table_args__ = (
        UniqueConstraint(
            "risk_episode_id",
            "symbol",
            "target_generation",
            name="uq_risk_episode_target_generation_symbol",
        ),
        CheckConstraint("target_quantity >= 0", name="ck_risk_target_nonnegative"),
    )

    risk_target_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    risk_episode_id: Mapped[str] = mapped_column(
        ForeignKey("risk_episodes.risk_episode_id", ondelete="RESTRICT")
    )
    symbol: Mapped[str] = mapped_column(String(30))
    target_generation: Mapped[int] = mapped_column(Integer)
    target_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    trigger_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    trigger_quote_id: Mapped[str] = mapped_column(String(100))
    target_weight: Mapped[Decimal | None] = mapped_column(Numeric(20, 12))
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    target_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RiskEpisodeEventRow(Base):
    __tablename__ = "risk_episode_events"
    __table_args__ = (
        UniqueConstraint(
            "risk_episode_id",
            "event_sequence",
            name="uq_risk_episode_event_sequence",
        ),
        CheckConstraint(
            "event_type IN "
            "('ACTIVATE', 'ESCALATE', 'TARGET_PROGRESS', 'TARGET_REACHED', 'RELEASE')",
            name="ck_risk_episode_event_type",
        ),
    )

    risk_episode_event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    risk_episode_id: Mapped[str] = mapped_column(
        ForeignKey("risk_episodes.risk_episode_id", ondelete="RESTRICT")
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    arm_id: Mapped[str] = mapped_column(String(30))
    source_cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT")
    )
    risk_target_id: Mapped[str | None] = mapped_column(
        ForeignKey("risk_episode_targets.risk_target_id", ondelete="RESTRICT")
    )
    algorithm_version: Mapped[str] = mapped_column(String(80))
    event_sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(30))
    severity: Mapped[str] = mapped_column(String(30))
    target_generation: Mapped[int] = mapped_column(Integer)
    observed_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    residual_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    consecutive_valid_checks: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    worker_fence_token: Mapped[str] = mapped_column(String(120))
    cycle_attempt_count: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(120))
    source_manifest_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderEventRow(Base):
    __tablename__ = "order_events"
    __table_args__ = (
        UniqueConstraint(
            "order_intent_id",
            "event_sequence",
            name="uq_order_event_sequence",
        ),
        CheckConstraint(
            "event_type IN "
            "('CREATED', 'ACTIVE', 'PARTIALLY_FILLED', 'FILLED', "
            "'CANCELED_BY_RISK', 'SUPERSEDED', 'EXPIRED', 'REJECTED', "
            "'BLOCKED_BY_DATA', 'BLOCKED_BY_PRICE_GUARD')",
            name="ck_order_event_type",
        ),
        CheckConstraint("remaining_quantity >= 0", name="ck_order_event_remaining"),
    )

    order_event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    order_intent_id: Mapped[str] = mapped_column(
        ForeignKey("order_intents.order_intent_id", ondelete="RESTRICT")
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    arm_id: Mapped[str] = mapped_column(String(30))
    source_cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT")
    )
    algorithm_version: Mapped[str] = mapped_column(String(80))
    event_sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(40))
    quantity_delta: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    commission_delta_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    remaining_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    cumulative_filled_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    cumulative_commission_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    quote_id: Mapped[str | None] = mapped_column(String(100))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(160))
    source_id: Mapped[str | None] = mapped_column(String(100))
    worker_fence_token: Mapped[str] = mapped_column(String(120))
    cycle_attempt_count: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(120))
    source_manifest_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CashSettlementEventRow(Base):
    __tablename__ = "cash_settlement_events"
    __table_args__ = (
        UniqueConstraint(
            "receivable_id",
            "event_type",
            name="uq_cash_receivable_event_type",
        ),
        UniqueConstraint(
            "source_fill_id",
            "event_type",
            name="uq_cash_fill_event_type",
        ),
        CheckConstraint(
            "event_type IN "
            "('OPENING_SETTLED_CASH', 'BUY_SETTLED_CASH_DEBIT', "
            "'SELL_RECEIVABLE_CREATED', 'RECEIVABLE_SETTLED')",
            name="ck_cash_settlement_event_type",
        ),
    )

    cash_settlement_event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    arm_id: Mapped[str] = mapped_column(String(30))
    source_fill_id: Mapped[str | None] = mapped_column(
        ForeignKey("fills.fill_id", ondelete="RESTRICT")
    )
    calendar_session_id: Mapped[str] = mapped_column(
        ForeignKey("market_calendar_sessions.calendar_session_id", ondelete="RESTRICT")
    )
    source_cycle_id: Mapped[str] = mapped_column(
        ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT")
    )
    algorithm_version: Mapped[str] = mapped_column(String(80))
    event_type: Mapped[str] = mapped_column(String(40))
    receivable_id: Mapped[str | None] = mapped_column(String(100))
    settlement_policy_version: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(3))
    settled_cash_delta_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    unsettled_receivable_delta_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    gross_amount_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    commission_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settlement_date: Mapped[date | None] = mapped_column(Date)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    worker_fence_token: Mapped[str] = mapped_column(String(120))
    cycle_attempt_count: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(120))
    source_manifest_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategyDailyResultRow(Base):
    __tablename__ = "strategy_daily_results"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "arm_id",
            "session_date",
            name="uq_strategy_daily_result",
        ),
    )

    strategy_daily_result_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    evaluation_anchor_id: Mapped[str] = mapped_column(
        ForeignKey(
            "strategy_evaluation_anchors.evaluation_anchor_id",
            ondelete="RESTRICT",
        )
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    arm_id: Mapped[str] = mapped_column(String(30))
    calendar_session_id: Mapped[str] = mapped_column(
        ForeignKey("market_calendar_sessions.calendar_session_id", ondelete="RESTRICT")
    )
    algorithm_version: Mapped[str] = mapped_column(String(80))
    session_date: Mapped[date] = mapped_column(Date)
    valuation_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    nav_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    net_daily_return: Mapped[Decimal] = mapped_column(Numeric(24, 14))
    cumulative_return: Mapped[Decimal] = mapped_column(Numeric(24, 14))
    daily_turnover: Mapped[Decimal] = mapped_column(Numeric(24, 14))
    cumulative_turnover: Mapped[Decimal] = mapped_column(Numeric(24, 14))
    commissions_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    spread_cost_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    delay_cost_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    sensitivity_5bp_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    sensitivity_10bp_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    cash_weight: Mapped[Decimal] = mapped_column(Numeric(20, 12))
    qqq_weight: Mapped[Decimal] = mapped_column(Numeric(20, 12))
    soxx_weight: Mapped[Decimal] = mapped_column(Numeric(20, 12))
    active_risk_episode_count: Mapped[int] = mapped_column(Integer)
    active_llm_reduction_count: Mapped[int] = mapped_column(Integer)
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(120))
    source_manifest_hash: Mapped[str] = mapped_column(String(64))
    result_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MatchedAttributionResultRow(Base):
    __tablename__ = "matched_attribution_results"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "comparison",
            "through_session_date",
            name="uq_matched_attribution_result",
        ),
        CheckConstraint(
            "comparison IN ('Q1_DET_MINUS_B0_VOL', 'Q1_LLM_MINUS_Q1_DET')",
            name="ck_matched_attribution_comparison",
        ),
    )

    matched_attribution_result_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    evaluation_anchor_id: Mapped[str] = mapped_column(
        ForeignKey(
            "strategy_evaluation_anchors.evaluation_anchor_id",
            ondelete="RESTRICT",
        )
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    algorithm_version: Mapped[str] = mapped_column(String(80))
    comparison: Mapped[str] = mapped_column(String(40))
    left_arm_id: Mapped[str] = mapped_column(String(30))
    right_arm_id: Mapped[str] = mapped_column(String(30))
    through_session_date: Mapped[date] = mapped_column(Date)
    common_valid_sessions: Mapped[int] = mapped_column(Integer)
    mean_daily_difference: Mapped[Decimal] = mapped_column(Numeric(24, 14))
    annualized_difference: Mapped[Decimal] = mapped_column(Numeric(24, 14))
    newey_west_lag: Mapped[int] = mapped_column(Integer)
    newey_west_standard_error: Mapped[Decimal] = mapped_column(Numeric(24, 14))
    bootstrap_seed: Mapped[int] = mapped_column(Integer)
    bootstrap_lower: Mapped[Decimal] = mapped_column(Numeric(24, 14))
    bootstrap_upper: Mapped[Decimal] = mapped_column(Numeric(24, 14))
    promotion_ready: Mapped[bool] = mapped_column(Boolean)
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(120))
    source_manifest_hash: Mapped[str] = mapped_column(String(64))
    result_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperBrokerBindingRow(Base):
    __tablename__ = "paper_broker_bindings"

    binding_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id", ondelete="RESTRICT"),
        unique=True,
    )
    execution_lane: Mapped[str] = mapped_column(String(40))
    source_arm_id: Mapped[str] = mapped_column(String(30))
    provider: Mapped[str] = mapped_column(String(20))
    account_id_hash: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(String(120))
    initial_equity_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    initial_cash_usd: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(80))
    binding_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperBrokerCommandRow(Base):
    __tablename__ = "paper_broker_commands"

    command_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    binding_id: Mapped[str] = mapped_column(
        ForeignKey(
            "paper_broker_bindings.binding_id",
            ondelete="RESTRICT",
        )
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    source_decision_id: Mapped[str] = mapped_column(
        ForeignKey(
            "portfolio_decisions.portfolio_decision_id",
            ondelete="RESTRICT",
        )
    )
    command_type: Mapped[str] = mapped_column(String(20))
    client_order_id: Mapped[str | None] = mapped_column(String(128))
    broker_order_id: Mapped[str | None] = mapped_column(String(100))
    symbol: Mapped[str] = mapped_column(String(30))
    side: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 10))
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    reason: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(80))
    source_manifest_hash: Mapped[str] = mapped_column(String(64))
    command_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperBrokerEventRow(Base):
    __tablename__ = "paper_broker_events"
    __table_args__ = (
        UniqueConstraint(
            "binding_id",
            "idempotency_key",
            name="uq_paper_broker_event_idempotency",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    binding_id: Mapped[str] = mapped_column(
        ForeignKey(
            "paper_broker_bindings.binding_id",
            ondelete="RESTRICT",
        )
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    command_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "paper_broker_commands.command_id",
            ondelete="RESTRICT",
        )
    )
    event_type: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(50))
    broker_order_id: Mapped[str | None] = mapped_column(String(100))
    client_order_id: Mapped[str | None] = mapped_column(String(128))
    provider_event_id: Mapped[str | None] = mapped_column(String(120))
    symbol: Mapped[str | None] = mapped_column(String(30))
    side: Mapped[str | None] = mapped_column(String(10))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    filled_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    fill_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider_request_id: Mapped[str | None] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(180))
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(80))
    source_manifest_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchCommanderSelectionRow(Base):
    __tablename__ = "research_commander_selections"

    selection_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    selected_commander: Mapped[str] = mapped_column(String(40))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    config_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchCycleRow(Base):
    __tablename__ = "research_cycles"

    research_cycle_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(100), unique=True)
    selection_id: Mapped[str] = mapped_column(
        ForeignKey(
            "research_commander_selections.selection_id",
            ondelete="RESTRICT",
        )
    )
    selection_version: Mapped[int] = mapped_column(Integer)
    selected_commander: Mapped[str] = mapped_column(String(40))
    source_snapshot_commit: Mapped[str] = mapped_column(String(64))
    champion_version: Mapped[str] = mapped_column(String(80))
    experiment_family: Mapped[str] = mapped_column(String(100))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_available_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    context_manifest_hash: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchCycleEventRow(Base):
    __tablename__ = "research_cycle_events"
    __table_args__ = (
        UniqueConstraint(
            "research_cycle_id",
            "idempotency_key",
            name="uq_research_cycle_event_idempotency",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    research_cycle_id: Mapped[str] = mapped_column(
        ForeignKey("research_cycles.research_cycle_id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(50))
    actor_role: Mapped[str] = mapped_column(String(40))
    artifact_hash: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchEvidenceSourceRow(Base):
    __tablename__ = "research_evidence_sources"
    __table_args__ = (
        UniqueConstraint(
            "research_cycle_id",
            "content_hash",
            name="uq_research_evidence_cycle_content",
        ),
    )

    source_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    research_cycle_id: Mapped[str] = mapped_column(
        ForeignKey("research_cycles.research_cycle_id", ondelete="RESTRICT")
    )
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(600))
    source_name: Mapped[str] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_tier: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64))
    excerpt: Mapped[str] = mapped_column(String(2000))
    license_note: Mapped[str] = mapped_column(String(500))
    corroborated: Mapped[bool] = mapped_column(Boolean)
    contradiction: Mapped[bool] = mapped_column(Boolean)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlgorithmProposalRow(Base):
    __tablename__ = "algorithm_proposals"

    proposal_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    research_cycle_id: Mapped[str] = mapped_column(
        ForeignKey("research_cycles.research_cycle_id", ondelete="RESTRICT")
    )
    hypothesis_id: Mapped[str] = mapped_column(String(100))
    parent_strategy_id: Mapped[str] = mapped_column(String(100))
    parent_strategy_version: Mapped[str] = mapped_column(String(80))
    proposed_strategy_id: Mapped[str] = mapped_column(String(100))
    proposed_strategy_version: Mapped[str] = mapped_column(String(80))
    proposal_hash: Mapped[str] = mapped_column(String(64), unique=True)
    evidence_manifest_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChallengerManifestRow(Base):
    __tablename__ = "challenger_manifests"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id",
            "strategy_version",
            name="uq_challenger_strategy_version",
        ),
    )

    challenger_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("algorithm_proposals.proposal_id", ondelete="RESTRICT")
    )
    strategy_id: Mapped[str] = mapped_column(String(100))
    strategy_version: Mapped[str] = mapped_column(String(80))
    parent_version: Mapped[str] = mapped_column(String(80))
    experiment_family: Mapped[str] = mapped_column(String(100))
    source_commit: Mapped[str] = mapped_column(String(64))
    patch_hash: Mapped[str] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    test_manifest_hash: Mapped[str] = mapped_column(String(64))
    initial_status: Mapped[str] = mapped_column(String(40))
    manifest_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchCandidateArtifactRow(Base):
    __tablename__ = "research_candidate_artifacts"
    __table_args__ = (
        CheckConstraint(
            "real_order_routing = false",
            name="ck_research_candidate_artifact_paper_only",
        ),
    )

    bundle_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    challenger_id: Mapped[str] = mapped_column(
        ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
        unique=True,
    )
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("algorithm_proposals.proposal_id", ondelete="RESTRICT")
    )
    research_cycle_id: Mapped[str] = mapped_column(
        ForeignKey("research_cycles.research_cycle_id", ondelete="RESTRICT")
    )
    candidate_tree_hash: Mapped[str] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    test_manifest_hash: Mapped[str] = mapped_column(String(64))
    declared_entrypoint: Mapped[str] = mapped_column(String(512))
    bundle_hash: Mapped[str] = mapped_column(String(64), unique=True)
    real_order_routing: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchExperimentActionRow(Base):
    __tablename__ = "research_experiment_actions"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            name="uq_research_experiment_action_experiment",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_research_experiment_action_idempotency",
        ),
    )

    action_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(String(160))
    research_cycle_id: Mapped[str] = mapped_column(String(160))
    proposal_id: Mapped[str] = mapped_column(String(160))
    challenger_id: Mapped[str] = mapped_column(String(160))
    information_role: Mapped[str] = mapped_column(String(40))
    primary_action_kind: Mapped[str] = mapped_column(String(50))
    maturity_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    meta_training_permitted: Mapped[bool] = mapped_column(Boolean)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    action_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchExperimentOutcomeEventRow(Base):
    __tablename__ = "research_experiment_outcome_events"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "event_sequence",
            name="uq_research_experiment_outcome_sequence",
        ),
        UniqueConstraint(
            "experiment_id",
            "idempotency_key",
            name="uq_research_experiment_outcome_idempotency",
        ),
        CheckConstraint(
            "event_sequence >= 1",
            name="ck_research_experiment_outcome_sequence",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey(
            "research_experiment_actions.action_id",
            ondelete="RESTRICT",
        )
    )
    experiment_id: Mapped[str] = mapped_column(String(160))
    research_cycle_id: Mapped[str] = mapped_column(String(160))
    proposal_id: Mapped[str] = mapped_column(String(160))
    challenger_id: Mapped[str] = mapped_column(String(160))
    information_role: Mapped[str] = mapped_column(String(40))
    primary_action_kind: Mapped[str] = mapped_column(String(50))
    event_kind: Mapped[str] = mapped_column(String(60))
    experiment_stage: Mapped[str] = mapped_column(String(40))
    event_sequence: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    maturity_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    maturity_status: Mapped[str] = mapped_column(String(30))
    eligible_for_meta_training: Mapped[bool] = mapped_column(Boolean)
    previous_event_hash: Mapped[str | None] = mapped_column(String(64))
    supersedes_event_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "research_experiment_outcome_events.event_id",
            ondelete="RESTRICT",
        )
    )
    idempotency_key: Mapped[str] = mapped_column(String(160))
    maturation_input_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchMemorySnapshotRow(Base):
    __tablename__ = "research_memory_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_available_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True)
    )
    snapshot_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChallengerEventRow(Base):
    __tablename__ = "challenger_events"
    __table_args__ = (
        UniqueConstraint(
            "challenger_id",
            "idempotency_key",
            name="uq_challenger_event_idempotency",
        ),
        UniqueConstraint(
            "challenger_id",
            "sequence",
            name="uq_challenger_event_sequence",
        ),
    )

    challenger_event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    challenger_id: Mapped[str] = mapped_column(
        ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    from_status: Mapped[str] = mapped_column(String(40))
    to_status: Mapped[str] = mapped_column(String(40))
    reason_code: Mapped[str] = mapped_column(String(100))
    artifact_hash: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExperimentBudgetEventRow(Base):
    __tablename__ = "experiment_budget_events"
    __table_args__ = (
        UniqueConstraint(
            "experiment_family",
            "idempotency_key",
            name="uq_experiment_budget_event_idempotency",
        ),
    )

    budget_event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    experiment_family: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(40))
    submission_delta: Mapped[int] = mapped_column(Integer)
    oos_budget_delta: Mapped[int] = mapped_column(Integer)
    hypothesis_delta: Mapped[int] = mapped_column(Integer)
    failure_delta: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OosBudgetReservationRow(Base):
    __tablename__ = "oos_budget_reservations"
    __table_args__ = (
        UniqueConstraint(
            "experiment_family",
            "idempotency_key",
            name="uq_oos_budget_reservation_idempotency",
        ),
        UniqueConstraint(
            "experiment_family",
            "submission_number",
            name="uq_oos_budget_reservation_submission",
        ),
        UniqueConstraint(
            "experiment_family",
            "submission_ordinal",
            name="uq_oos_budget_reservation_submission_ordinal",
        ),
        UniqueConstraint(
            "experiment_family",
            "oos_budget_ordinal",
            name="uq_oos_budget_reservation_budget_ordinal",
        ),
    )

    reservation_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    challenger_id: Mapped[str] = mapped_column(
        ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT")
    )
    experiment_family: Mapped[str] = mapped_column(String(100))
    submission_number: Mapped[int] = mapped_column(Integer)
    submission_ordinal: Mapped[int] = mapped_column(Integer)
    oos_budget_ordinal: Mapped[int] = mapped_column(Integer)
    candidate_artifact_hash: Mapped[str] = mapped_column(String(64))
    evaluation_contract_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    reservation_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FalsificationReportRow(Base):
    __tablename__ = "falsification_reports"

    falsification_report_id: Mapped[str] = mapped_column(
        String(100),
        primary_key=True,
    )
    challenger_id: Mapped[str] = mapped_column(
        ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
        unique=True,
    )
    mandatory_passed: Mapped[bool] = mapped_column(Boolean)
    report_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchReplayArtifactRow(Base):
    __tablename__ = "research_replay_artifacts"

    replay_artifact_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    challenger_id: Mapped[str] = mapped_column(
        ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
        unique=True,
    )
    candidate_artifact_hash: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(64))
    data_manifest_hash: Mapped[str] = mapped_column(String(64))
    first_replay_hash: Mapped[str] = mapped_column(String(64))
    second_replay_hash: Mapped[str] = mapped_column(String(64))
    deterministic_match: Mapped[bool] = mapped_column(Boolean)
    artifact_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OosLockboxResultRow(Base):
    __tablename__ = "oos_lockbox_results"
    __table_args__ = (
        UniqueConstraint(
            "challenger_id",
            "submission_number",
            name="uq_oos_lockbox_challenger_submission",
        ),
    )

    oos_result_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    challenger_id: Mapped[str] = mapped_column(
        ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT")
    )
    experiment_family: Mapped[str] = mapped_column(String(100))
    submission_number: Mapped[int] = mapped_column(Integer)
    candidate_artifact_hash: Mapped[str] = mapped_column(String(64))
    evaluation_contract_hash: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(20))
    common_sessions: Mapped[int] = mapped_column(Integer)
    result_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchShadowArmRegistrationRow(Base):
    __tablename__ = "research_shadow_arm_registrations"
    __table_args__ = (
        UniqueConstraint(
            "shadow_pair_id",
            "arm_role",
            name="uq_research_shadow_pair_role",
        ),
        UniqueConstraint(
            "challenger_id",
            "arm_role",
            name="uq_research_shadow_challenger_role",
        ),
    )

    shadow_registration_id: Mapped[str] = mapped_column(
        String(100),
        primary_key=True,
    )
    shadow_pair_id: Mapped[str] = mapped_column(String(100))
    challenger_id: Mapped[str] = mapped_column(
        ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT")
    )
    oos_result_id: Mapped[str] = mapped_column(
        ForeignKey("oos_lockbox_results.oos_result_id", ondelete="RESTRICT")
    )
    arm_role: Mapped[str] = mapped_column(String(20))
    arm_id: Mapped[str] = mapped_column(String(100), unique=True)
    strategy_id: Mapped[str] = mapped_column(String(100))
    strategy_version: Mapped[str] = mapped_column(String(80))
    execution_contract_hash: Mapped[str] = mapped_column(String(64))
    real_order_routing: Mapped[bool] = mapped_column(Boolean)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchShadowPerformanceSummaryRow(Base):
    __tablename__ = "research_shadow_performance_summaries"
    __table_args__ = (
        UniqueConstraint(
            "challenger_id",
            "materialized_evidence_hash",
            name="uq_research_shadow_summary_materialized",
        ),
    )

    summary_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    challenger_id: Mapped[str] = mapped_column(
        ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT")
    )
    shadow_pair_id: Mapped[str] = mapped_column(String(100))
    run_id: Mapped[str] = mapped_column(String(100))
    source_summary_hash: Mapped[str] = mapped_column(String(64), unique=True)
    materialized_evidence_hash: Mapped[str] = mapped_column(String(64))
    summary_hash: Mapped[str] = mapped_column(String(64), unique=True)
    common_sessions: Mapped[int] = mapped_column(Integer)
    data_available_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchPromotionEvidenceRow(Base):
    __tablename__ = "research_promotion_evidence"

    evidence_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    challenger_id: Mapped[str] = mapped_column(
        ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT")
    )
    shadow_summary_id: Mapped[str] = mapped_column(
        ForeignKey(
            "research_shadow_performance_summaries.summary_id",
            ondelete="RESTRICT",
        )
    )
    evidence_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TrustedPromotionEvaluationRow(Base):
    __tablename__ = "trusted_promotion_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "evidence_hash",
            "contract_hash",
            name="uq_trusted_promotion_evidence_contract",
        ),
    )

    evaluation_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("research_promotion_evidence.evidence_id", ondelete="RESTRICT")
    )
    challenger_id: Mapped[str] = mapped_column(
        ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT")
    )
    promotion_decision_id: Mapped[str] = mapped_column(
        ForeignKey(
            "research_promotion_decisions.promotion_decision_id",
            ondelete="RESTRICT",
        )
    )
    evidence_hash: Mapped[str] = mapped_column(String(64))
    contract_hash: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(50))
    evaluation_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchChampionDesignationRow(Base):
    __tablename__ = "research_champion_designations"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id",
            "strategy_version",
            name="uq_research_champion_strategy_version",
        ),
    )

    designation_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, unique=True)
    strategy_id: Mapped[str] = mapped_column(String(100))
    strategy_version: Mapped[str] = mapped_column(String(80))
    candidate_artifact_hash: Mapped[str] = mapped_column(String(64))
    source_challenger_id: Mapped[str] = mapped_column(
        ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT")
    )
    trusted_evaluation_id: Mapped[str] = mapped_column(
        ForeignKey(
            "trusted_promotion_evaluations.evaluation_id",
            ondelete="RESTRICT",
        )
    )
    manual_approval_decision_id: Mapped[str] = mapped_column(
        ForeignKey(
            "research_promotion_decisions.promotion_decision_id",
            ondelete="RESTRICT",
        )
    )
    previous_designation_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "research_champion_designations.designation_id",
            ondelete="RESTRICT",
        )
    )
    expected_current_version: Mapped[str] = mapped_column(String(80))
    designated_by: Mapped[str] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    automatic_promotion_enabled: Mapped[bool] = mapped_column(Boolean)
    real_order_routing: Mapped[bool] = mapped_column(Boolean)
    designation_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    designated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchPromotionDecisionRow(Base):
    __tablename__ = "research_promotion_decisions"

    promotion_decision_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    challenger_id: Mapped[str] = mapped_column(
        ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT")
    )
    verdict: Mapped[str] = mapped_column(String(50))
    automatic_promotion_enabled: Mapped[bool] = mapped_column(Boolean)
    replay_hash: Mapped[str] = mapped_column(String(64))
    decision_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


APPEND_ONLY_MODEL_TYPES = (
    DomainEventRow,
    SourceRecordRow,
    MarketBarRow,
    MarketQuoteRow,
    MarketTradeEventRow,
    FeatureSnapshotRow,
    StrategyForecastRow,
    NewsEventRow,
    PolicyPatchRow,
    PolicyVersionRow,
    CommanderSelectionRow,
    CommanderRequestRow,
    CommanderDecisionRow,
    CommanderDecisionResultRow,
    PortfolioDecisionRow,
    RiskDecisionRow,
    OrderIntentRow,
    FillRow,
    LedgerTransactionRow,
    LedgerPostingRow,
    NavSnapshotRow,
    PaperAccountSpecRow,
    PaperCashBalanceRow,
    PaperPositionRow,
    PaperBootstrapMarkRow,
    PaperBootstrapCompletionRow,
    ArmStateSnapshotRow,
    PaperCycleEffectRow,
    PaperExecutionAttemptRow,
    ForwardStrategyCandidateRow,
    ForecastCalibrationRow,
    StrategyPromotionDecisionRow,
    MarketCalendarSessionRow,
    StrategyEvaluationAnchorRow,
    RiskEpisodeRow,
    RiskEpisodeTargetRow,
    RiskEpisodeEventRow,
    OrderEventRow,
    CashSettlementEventRow,
    StrategyDailyResultRow,
    MatchedAttributionResultRow,
    PaperBrokerBindingRow,
    PaperBrokerCommandRow,
    PaperBrokerEventRow,
    ResearchCommanderSelectionRow,
    ResearchCycleRow,
    ResearchCycleEventRow,
    ResearchEvidenceSourceRow,
    AlgorithmProposalRow,
    ChallengerManifestRow,
    ResearchCandidateArtifactRow,
    ResearchExperimentActionRow,
    ResearchExperimentOutcomeEventRow,
    ResearchMemorySnapshotRow,
    ChallengerEventRow,
    ExperimentBudgetEventRow,
    OosBudgetReservationRow,
    FalsificationReportRow,
    ResearchReplayArtifactRow,
    OosLockboxResultRow,
    ResearchShadowArmRegistrationRow,
    ResearchShadowPerformanceSummaryRow,
    ResearchPromotionEvidenceRow,
    TrustedPromotionEvaluationRow,
    ResearchChampionDesignationRow,
    ResearchPromotionDecisionRow,
)


class AppendOnlyViolation(RuntimeError):
    pass


@event.listens_for(Session, "before_flush")
def prevent_orm_mutation(session: Session, *_: object) -> None:
    for obj in session.dirty:
        if isinstance(obj, APPEND_ONLY_MODEL_TYPES) and session.is_modified(
            obj, include_collections=False
        ):
            raise AppendOnlyViolation(f"{type(obj).__name__} is append-only")
    for obj in session.deleted:
        if isinstance(obj, APPEND_ONLY_MODEL_TYPES):
            raise AppendOnlyViolation(f"{type(obj).__name__} is append-only")
