"""Typed persistence foundation for q1_math_core_v1.

Revision ID: 0007_q1_math_core_v1
Revises: 0006_forward_paper_execution
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_q1_math_core_v1"
down_revision: str | None = "0006_forward_paper_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AFFECTED_APPEND_ONLY_TABLES = (
    "portfolio_decisions",
    "order_intents",
    "fills",
    "nav_snapshots",
)
NEW_APPEND_ONLY_TABLES = (
    "market_calendar_sessions",
    "strategy_evaluation_anchors",
    "risk_episodes",
    "risk_episode_targets",
    "risk_episode_events",
    "order_events",
    "cash_settlement_events",
    "strategy_daily_results",
    "matched_attribution_results",
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _drop_sqlite_guards(AFFECTED_APPEND_ONLY_TABLES)

    op.create_table(
        "market_calendar_sessions",
        sa.Column("calendar_session_id", sa.String(100), primary_key=True),
        sa.Column("algorithm_version", sa.String(80), nullable=False),
        sa.Column("calendar_version", sa.String(80), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("open_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(80), nullable=False),
        sa.Column("model_version", sa.String(120), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=False),
        sa.Column("session_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "close_at > open_at",
            name="ck_market_calendar_positive_session",
        ),
        sa.UniqueConstraint(
            "calendar_version",
            "session_date",
            "session_hash",
            name="uq_market_calendar_version_date_hash",
        ),
    )
    op.create_index(
        "ix_market_calendar_session_pit",
        "market_calendar_sessions",
        ("session_date", "available_at"),
    )

    with op.batch_alter_table("portfolio_decisions") as batch:
        batch.add_column(sa.Column("algorithm_version", sa.String(80)))
        batch.add_column(sa.Column("scheduled_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("signal_data_cutoff", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("portfolio_state_as_of", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("quote_as_of", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("decision_created_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("valid_until", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("calendar_session_id", sa.String(100)))
        batch.add_column(sa.Column("config_manifest_hash", sa.String(64)))
        batch.add_column(sa.Column("code_version", sa.String(80)))
        batch.add_column(sa.Column("model_version", sa.String(120)))
        batch.add_column(sa.Column("source_manifest_hash", sa.String(64)))
        batch.add_column(sa.Column("input_manifest_hash", sa.String(64)))
        batch.create_foreign_key(
            "fk_portfolio_decision_calendar_session",
            "market_calendar_sessions",
            ["calendar_session_id"],
            ["calendar_session_id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("order_intents") as batch:
        batch.add_column(sa.Column("algorithm_version", sa.String(80)))
        batch.add_column(sa.Column("config_manifest_hash", sa.String(64)))
        batch.add_column(sa.Column("code_version", sa.String(80)))
        batch.add_column(sa.Column("model_version", sa.String(120)))
        batch.add_column(sa.Column("source_manifest_hash", sa.String(64)))
        batch.add_column(sa.Column("decision_spread_bps", sa.Numeric(20, 10)))
    with op.batch_alter_table("fills") as batch:
        batch.add_column(sa.Column("algorithm_version", sa.String(80)))
        batch.add_column(sa.Column("config_manifest_hash", sa.String(64)))
        batch.add_column(sa.Column("code_version", sa.String(80)))
        batch.add_column(sa.Column("model_version", sa.String(120)))
        batch.add_column(sa.Column("source_manifest_hash", sa.String(64)))
        batch.add_column(sa.Column("base_fill_cost_usd", sa.Numeric(38, 10)))
        batch.add_column(sa.Column("sensitivity_5bp_cost_usd", sa.Numeric(38, 10)))
        batch.add_column(sa.Column("sensitivity_10bp_cost_usd", sa.Numeric(38, 10)))
    with op.batch_alter_table("nav_snapshots") as batch:
        batch.add_column(sa.Column("algorithm_version", sa.String(80)))
        batch.add_column(sa.Column("config_manifest_hash", sa.String(64)))
        batch.add_column(sa.Column("code_version", sa.String(80)))
        batch.add_column(sa.Column("model_version", sa.String(120)))
        batch.add_column(sa.Column("source_manifest_hash", sa.String(64)))

    op.create_table(
        "strategy_evaluation_anchors",
        sa.Column("evaluation_anchor_id", sa.String(100), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("algorithm_version", sa.String(80), nullable=False),
        sa.Column(
            "calendar_session_id",
            sa.String(100),
            sa.ForeignKey(
                "market_calendar_sessions.calendar_session_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("common_t0_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("initial_nav_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("quote_manifest_hash", sa.String(64), nullable=False),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(80), nullable=False),
        sa.Column("model_version", sa.String(120), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=False),
        sa.Column("anchor_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "risk_episodes",
        sa.Column("risk_episode_id", sa.String(100), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column("algorithm_version", sa.String(80), nullable=False),
        sa.Column("severity", sa.String(30), nullable=False),
        sa.Column(
            "calendar_session_id",
            sa.String(100),
            sa.ForeignKey(
                "market_calendar_sessions.calendar_session_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger_nav_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("session_open_nav_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("running_peak_nav_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("daily_loss", sa.Numeric(20, 12), nullable=False),
        sa.Column("run_drawdown", sa.Numeric(20, 12), nullable=False),
        sa.Column("portfolio_annualized_vol", sa.Numeric(20, 12)),
        sa.Column("soft_daily_threshold", sa.Numeric(20, 12), nullable=False),
        sa.Column("hard_daily_threshold", sa.Numeric(20, 12), nullable=False),
        sa.Column("reconciliation_status", sa.String(40), nullable=False),
        sa.Column("target_manifest_hash", sa.String(64), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(80), nullable=False),
        sa.Column("model_version", sa.String(120), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=False),
        sa.Column("episode_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "severity IN ('HARD_REDUCE', 'CRITICAL_EXIT')",
            name="ck_risk_episode_severity",
        ),
        sa.CheckConstraint("target_count > 0", name="ck_risk_episode_nonempty_targets"),
        sa.UniqueConstraint(
            "run_id",
            "arm_id",
            "episode_hash",
            name="uq_risk_episode_identity",
        ),
    )
    op.create_index(
        "ix_risk_episode_arm_time",
        "risk_episodes",
        ("run_id", "arm_id", "triggered_at"),
    )
    op.create_table(
        "risk_episode_targets",
        sa.Column("risk_target_id", sa.String(100), primary_key=True),
        sa.Column(
            "risk_episode_id",
            sa.String(100),
            sa.ForeignKey("risk_episodes.risk_episode_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("target_generation", sa.Integer(), nullable=False),
        sa.Column("target_quantity", sa.Numeric(38, 10), nullable=False),
        sa.Column("trigger_quantity", sa.Numeric(38, 10)),
        sa.Column("trigger_price", sa.Numeric(38, 12)),
        sa.Column("trigger_quote_id", sa.String(100), nullable=False),
        sa.Column("target_weight", sa.Numeric(20, 12)),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("target_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_quantity >= 0",
            name="ck_risk_target_nonnegative",
        ),
        sa.UniqueConstraint(
            "risk_episode_id",
            "symbol",
            "target_generation",
            name="uq_risk_episode_target_generation_symbol",
        ),
    )
    op.create_table(
        "risk_episode_events",
        sa.Column("risk_episode_event_id", sa.String(100), primary_key=True),
        sa.Column(
            "risk_episode_id",
            sa.String(100),
            sa.ForeignKey("risk_episodes.risk_episode_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column(
            "source_cycle_id",
            sa.String(100),
            sa.ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "risk_target_id",
            sa.String(100),
            sa.ForeignKey("risk_episode_targets.risk_target_id", ondelete="RESTRICT"),
        ),
        sa.Column("algorithm_version", sa.String(80), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("severity", sa.String(30), nullable=False),
        sa.Column("target_generation", sa.Integer(), nullable=False),
        sa.Column("observed_quantity", sa.Numeric(38, 10)),
        sa.Column("residual_quantity", sa.Numeric(38, 10)),
        sa.Column("consecutive_valid_checks", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("worker_fence_token", sa.String(120), nullable=False),
        sa.Column("cycle_attempt_count", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(180), nullable=False, unique=True),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(80), nullable=False),
        sa.Column("model_version", sa.String(120), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN "
            "('ACTIVATE', 'ESCALATE', 'TARGET_PROGRESS', 'TARGET_REACHED', 'RELEASE')",
            name="ck_risk_episode_event_type",
        ),
        sa.UniqueConstraint(
            "risk_episode_id",
            "event_sequence",
            name="uq_risk_episode_event_sequence",
        ),
    )
    op.create_index(
        "ix_risk_episode_event_latest",
        "risk_episode_events",
        ("risk_episode_id", "event_sequence"),
    )
    op.create_table(
        "order_events",
        sa.Column("order_event_id", sa.String(100), primary_key=True),
        sa.Column(
            "order_intent_id",
            sa.String(80),
            sa.ForeignKey("order_intents.order_intent_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column(
            "source_cycle_id",
            sa.String(100),
            sa.ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT"),
        ),
        sa.Column("algorithm_version", sa.String(80), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("quantity_delta", sa.Numeric(38, 10), nullable=False),
        sa.Column("commission_delta_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("remaining_quantity", sa.Numeric(38, 10), nullable=False),
        sa.Column("cumulative_filled_quantity", sa.Numeric(38, 10), nullable=False),
        sa.Column("cumulative_commission_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("quote_id", sa.String(100)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(160)),
        sa.Column("source_id", sa.String(100)),
        sa.Column("worker_fence_token", sa.String(120), nullable=False),
        sa.Column("cycle_attempt_count", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(180), nullable=False, unique=True),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(80), nullable=False),
        sa.Column("model_version", sa.String(120), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN "
            "('CREATED', 'ACTIVE', 'PARTIALLY_FILLED', 'FILLED', "
            "'CANCELED_BY_RISK', 'SUPERSEDED', 'EXPIRED', 'REJECTED', "
            "'BLOCKED_BY_DATA', 'BLOCKED_BY_PRICE_GUARD')",
            name="ck_order_event_type",
        ),
        sa.CheckConstraint(
            "remaining_quantity >= 0",
            name="ck_order_event_remaining",
        ),
        sa.UniqueConstraint(
            "order_intent_id",
            "event_sequence",
            name="uq_order_event_sequence",
        ),
    )
    op.create_index(
        "ix_order_event_pending_projection",
        "order_events",
        ("run_id", "arm_id", "order_intent_id", "event_sequence"),
    )
    op.create_table(
        "cash_settlement_events",
        sa.Column("cash_settlement_event_id", sa.String(100), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column(
            "source_fill_id",
            sa.String(80),
            sa.ForeignKey("fills.fill_id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "calendar_session_id",
            sa.String(100),
            sa.ForeignKey(
                "market_calendar_sessions.calendar_session_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "source_cycle_id",
            sa.String(100),
            sa.ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("algorithm_version", sa.String(80), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("receivable_id", sa.String(100)),
        sa.Column("settlement_policy_version", sa.String(80), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("settled_cash_delta_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column(
            "unsettled_receivable_delta_usd",
            sa.Numeric(38, 10),
            nullable=False,
        ),
        sa.Column("gross_amount_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("commission_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("trade_at", sa.DateTime(timezone=True)),
        sa.Column("settlement_date", sa.Date()),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("worker_fence_token", sa.String(120), nullable=False),
        sa.Column("cycle_attempt_count", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(180), nullable=False, unique=True),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(80), nullable=False),
        sa.Column("model_version", sa.String(120), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN "
            "('OPENING_SETTLED_CASH', 'BUY_SETTLED_CASH_DEBIT', "
            "'SELL_RECEIVABLE_CREATED', 'RECEIVABLE_SETTLED')",
            name="ck_cash_settlement_event_type",
        ),
        sa.UniqueConstraint(
            "receivable_id",
            "event_type",
            name="uq_cash_receivable_event_type",
        ),
        sa.UniqueConstraint(
            "source_fill_id",
            "event_type",
            name="uq_cash_fill_event_type",
        ),
    )
    op.create_index(
        "ix_cash_settlement_due",
        "cash_settlement_events",
        ("run_id", "arm_id", "settlement_date", "event_type"),
    )
    op.create_table(
        "strategy_daily_results",
        sa.Column("strategy_daily_result_id", sa.String(100), primary_key=True),
        sa.Column(
            "evaluation_anchor_id",
            sa.String(100),
            sa.ForeignKey(
                "strategy_evaluation_anchors.evaluation_anchor_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column(
            "calendar_session_id",
            sa.String(100),
            sa.ForeignKey(
                "market_calendar_sessions.calendar_session_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("algorithm_version", sa.String(80), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("valuation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("nav_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("net_daily_return", sa.Numeric(24, 14), nullable=False),
        sa.Column("cumulative_return", sa.Numeric(24, 14), nullable=False),
        sa.Column("daily_turnover", sa.Numeric(24, 14), nullable=False),
        sa.Column("cumulative_turnover", sa.Numeric(24, 14), nullable=False),
        sa.Column("commissions_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("spread_cost_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("delay_cost_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("sensitivity_5bp_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("sensitivity_10bp_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("cash_weight", sa.Numeric(20, 12), nullable=False),
        sa.Column("qqq_weight", sa.Numeric(20, 12), nullable=False),
        sa.Column("soxx_weight", sa.Numeric(20, 12), nullable=False),
        sa.Column("active_risk_episode_count", sa.Integer(), nullable=False),
        sa.Column("active_llm_reduction_count", sa.Integer(), nullable=False),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(80), nullable=False),
        sa.Column("model_version", sa.String(120), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "arm_id",
            "session_date",
            name="uq_strategy_daily_result",
        ),
    )
    op.create_table(
        "matched_attribution_results",
        sa.Column("matched_attribution_result_id", sa.String(100), primary_key=True),
        sa.Column(
            "evaluation_anchor_id",
            sa.String(100),
            sa.ForeignKey(
                "strategy_evaluation_anchors.evaluation_anchor_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("algorithm_version", sa.String(80), nullable=False),
        sa.Column("comparison", sa.String(40), nullable=False),
        sa.Column("left_arm_id", sa.String(30), nullable=False),
        sa.Column("right_arm_id", sa.String(30), nullable=False),
        sa.Column("through_session_date", sa.Date(), nullable=False),
        sa.Column("common_valid_sessions", sa.Integer(), nullable=False),
        sa.Column("mean_daily_difference", sa.Numeric(24, 14), nullable=False),
        sa.Column("annualized_difference", sa.Numeric(24, 14), nullable=False),
        sa.Column("newey_west_lag", sa.Integer(), nullable=False),
        sa.Column("newey_west_standard_error", sa.Numeric(24, 14), nullable=False),
        sa.Column("bootstrap_seed", sa.Integer(), nullable=False),
        sa.Column("bootstrap_lower", sa.Numeric(24, 14), nullable=False),
        sa.Column("bootstrap_upper", sa.Numeric(24, 14), nullable=False),
        sa.Column("promotion_ready", sa.Boolean(), nullable=False),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(80), nullable=False),
        sa.Column("model_version", sa.String(120), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "comparison IN ('Q1_DET_MINUS_B0_VOL', 'Q1_LLM_MINUS_Q1_DET')",
            name="ck_matched_attribution_comparison",
        ),
        sa.UniqueConstraint(
            "run_id",
            "comparison",
            "through_session_date",
            name="uq_matched_attribution_result",
        ),
    )

    if dialect == "sqlite":
        _create_sqlite_guards(
            (*AFFECTED_APPEND_ONLY_TABLES, *NEW_APPEND_ONLY_TABLES)
        )
    elif dialect == "postgresql":
        _create_postgres_guards(NEW_APPEND_ONLY_TABLES)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _drop_sqlite_guards(
            (*AFFECTED_APPEND_ONLY_TABLES, *NEW_APPEND_ONLY_TABLES)
        )
    elif dialect == "postgresql":
        _drop_postgres_guards(NEW_APPEND_ONLY_TABLES)

    op.drop_table("matched_attribution_results")
    op.drop_table("strategy_daily_results")
    op.drop_index("ix_cash_settlement_due", table_name="cash_settlement_events")
    op.drop_table("cash_settlement_events")
    op.drop_index("ix_order_event_pending_projection", table_name="order_events")
    op.drop_table("order_events")
    op.drop_index("ix_risk_episode_event_latest", table_name="risk_episode_events")
    op.drop_table("risk_episode_events")
    op.drop_table("risk_episode_targets")
    op.drop_index("ix_risk_episode_arm_time", table_name="risk_episodes")
    op.drop_table("risk_episodes")
    op.drop_table("strategy_evaluation_anchors")

    with op.batch_alter_table("nav_snapshots") as batch:
        for column in (
            "source_manifest_hash",
            "model_version",
            "code_version",
            "config_manifest_hash",
            "algorithm_version",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("fills") as batch:
        for column in (
            "sensitivity_10bp_cost_usd",
            "sensitivity_5bp_cost_usd",
            "base_fill_cost_usd",
            "source_manifest_hash",
            "model_version",
            "code_version",
            "config_manifest_hash",
            "algorithm_version",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("order_intents") as batch:
        for column in (
            "decision_spread_bps",
            "source_manifest_hash",
            "model_version",
            "code_version",
            "config_manifest_hash",
            "algorithm_version",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("portfolio_decisions") as batch:
        batch.drop_constraint(
            "fk_portfolio_decision_calendar_session",
            type_="foreignkey",
        )
        for column in (
            "input_manifest_hash",
            "source_manifest_hash",
            "model_version",
            "code_version",
            "config_manifest_hash",
            "calendar_session_id",
            "valid_until",
            "decision_created_at",
            "quote_as_of",
            "portfolio_state_as_of",
            "signal_data_cutoff",
            "scheduled_at",
            "algorithm_version",
        ):
            batch.drop_column(column)

    op.drop_index(
        "ix_market_calendar_session_pit",
        table_name="market_calendar_sessions",
    )
    op.drop_table("market_calendar_sessions")

    if dialect == "sqlite":
        _create_sqlite_guards(AFFECTED_APPEND_ONLY_TABLES)


def _drop_sqlite_guards(tables: Sequence[str]) -> None:
    for table in tables:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")


def _create_sqlite_guards(tables: Sequence[str]) -> None:
    for table in tables:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only table cannot be updated');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only table cannot be deleted');
            END
            """
        )


def _create_postgres_guards(tables: Sequence[str]) -> None:
    for table in tables:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()
            """
        )


def _drop_postgres_guards(tables: Sequence[str]) -> None:
    for table in tables:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
