"""Phase 0 append-only trading foundation.

Revision ID: 0001_phase0
Revises:
Create Date: 2026-07-26
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_phase0"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "domain_events",
    "source_records",
    "feature_snapshots",
    "strategy_forecasts",
    "news_events",
    "policy_patches",
    "portfolio_decisions",
    "risk_decisions",
    "order_intents",
    "fills",
    "ledger_transactions",
    "ledger_postings",
    "nav_snapshots",
)


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(80), primary_key=True),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("experiment_version", sa.String(80), nullable=False),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("code_commit", sa.String(80), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("result_manifest", sa.JSON()),
        sa.Column("result_hash", sa.String(64)),
    )
    op.create_table(
        "domain_events",
        sa.Column("event_id", sa.String(80), primary_key=True),
        sa.Column("aggregate_type", sa.String(80), nullable=False),
        sa.Column("aggregate_id", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("event_version", sa.String(20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("causation_id", sa.String(80)),
        sa.Column("correlation_id", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "outbox_events",
        sa.Column("outbox_id", sa.String(80), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(80),
            sa.ForeignKey("domain_events.event_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("topic", sa.String(100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "processed_events",
        sa.Column("consumer_name", sa.String(100), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(80),
            sa.ForeignKey("domain_events.event_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
    )
    op.create_table(
        "source_records",
        sa.Column("source_id", sa.String(80), primary_key=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("external_id", sa.String(120), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("provider", "external_id", "revision", name="uq_source_revision"),
    )
    op.create_table(
        "feature_snapshots",
        sa.Column("feature_snapshot_id", sa.String(80), primary_key=True),
        sa.Column("symbol", sa.String(30)),
        sa.Column("decision_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_available_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_manifest_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "strategy_forecasts",
        sa.Column("forecast_id", sa.String(80), primary_key=True),
        sa.Column("strategy_id", sa.String(40), nullable=False),
        sa.Column("strategy_version", sa.String(40), nullable=False),
        sa.Column("experiment_version", sa.String(80), nullable=False),
        sa.Column("decision_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon", sa.String(10), nullable=False),
        sa.Column("input_manifest_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "strategy_id",
            "strategy_version",
            "experiment_version",
            "decision_time",
            "horizon",
            "input_manifest_hash",
            name="uq_strategy_forecast_identity",
        ),
    )
    op.create_table(
        "news_events",
        sa.Column("news_event_id", sa.String(80), primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("output_hash", sa.String(64), nullable=False),
    )
    op.create_table(
        "policy_patches",
        sa.Column("patch_id", sa.String(80), primary_key=True),
        sa.Column("arm_scope", sa.String(30), nullable=False),
        sa.Column("base_policy_version", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "policy_versions",
        sa.Column("policy_version_id", sa.String(80), primary_key=True),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_patch_id", sa.String(80)),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("arm_id", "version", name="uq_arm_policy_version"),
    )
    op.create_table(
        "shadow_arms",
        sa.Column("arm_instance_id", sa.String(100), primary_key=True),
        sa.Column("run_id", sa.String(80), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "arm_id", name="uq_run_arm"),
    )
    op.create_table(
        "arm_state_snapshots",
        sa.Column("arm_state_snapshot_id", sa.String(100), primary_key=True),
        sa.Column("run_id", sa.String(80), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "arm_id", "sequence", name="uq_arm_state_sequence"),
    )
    op.create_table(
        "portfolio_decisions",
        sa.Column("portfolio_decision_id", sa.String(80), primary_key=True),
        sa.Column("run_id", sa.String(80), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column("decision_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False),
    )
    op.create_table(
        "risk_decisions",
        sa.Column("risk_decision_id", sa.String(80), primary_key=True),
        sa.Column(
            "portfolio_decision_id",
            sa.String(80),
            sa.ForeignKey("portfolio_decisions.portfolio_decision_id"),
            nullable=False,
        ),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "order_intents",
        sa.Column("order_intent_id", sa.String(80), primary_key=True),
        sa.Column("run_id", sa.String(80), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("intent_hash", sa.String(64), nullable=False),
    )
    op.create_table(
        "fills",
        sa.Column("fill_id", sa.String(80), primary_key=True),
        sa.Column(
            "order_intent_id",
            sa.String(80),
            sa.ForeignKey("order_intents.order_intent_id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(80), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "ledger_transactions",
        sa.Column("ledger_transaction_id", sa.String(80), primary_key=True),
        sa.Column("run_id", sa.String(80), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column("source_id", sa.String(80), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "ledger_postings",
        sa.Column("posting_id", sa.String(80), primary_key=True),
        sa.Column(
            "ledger_transaction_id",
            sa.String(80),
            sa.ForeignKey("ledger_transactions.ledger_transaction_id"),
            nullable=False,
        ),
        sa.Column("account_code", sa.String(80), nullable=False),
        sa.Column("asset_code", sa.String(30), nullable=False),
        sa.Column("quantity_delta", sa.Numeric(38, 10), nullable=False),
        sa.Column("usd_value_delta", sa.Numeric(38, 10), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "nav_snapshots",
        sa.Column("nav_snapshot_id", sa.String(80), primary_key=True),
        sa.Column("run_id", sa.String(80), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("arm_id", sa.String(30), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("nav_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
    )
    _create_append_only_guards()


def downgrade() -> None:
    for table in (
        "nav_snapshots",
        "ledger_postings",
        "ledger_transactions",
        "fills",
        "order_intents",
        "risk_decisions",
        "portfolio_decisions",
        "arm_state_snapshots",
        "shadow_arms",
        "policy_versions",
        "policy_patches",
        "news_events",
        "strategy_forecasts",
        "feature_snapshots",
        "source_records",
        "processed_events",
        "outbox_events",
        "domain_events",
        "runs",
    ):
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS reject_append_only_mutation()")


def _create_append_only_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION reject_append_only_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'append-only table % cannot be mutated', TG_TABLE_NAME;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in APPEND_ONLY_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()
                """
            )
    elif dialect == "sqlite":
        for table in APPEND_ONLY_TABLES:
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
