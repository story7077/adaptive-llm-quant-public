"""Forward paper decisions, quote-bound fills, and research candidates.

Revision ID: 0006_forward_paper_execution
Revises: 0005_policy_scope
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_forward_paper_execution"
down_revision: str | None = "0005_policy_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AFFECTED_APPEND_ONLY_TABLES = (
    "portfolio_decisions",
    "risk_decisions",
    "order_intents",
    "fills",
    "nav_snapshots",
)
NEW_APPEND_ONLY_TABLES = (
    "paper_cycle_effects",
    "paper_execution_attempts",
    "forward_strategy_candidates",
    "forecast_calibrations",
    "strategy_promotion_decisions",
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _drop_sqlite_guards(AFFECTED_APPEND_ONLY_TABLES)

    with op.batch_alter_table("portfolio_decisions") as batch:
        batch.add_column(sa.Column("source_cycle_id", sa.String(100)))
        batch.add_column(sa.Column("input_state_sequence", sa.Integer()))
        batch.create_foreign_key(
            "fk_portfolio_decision_source_cycle",
            "paper_cycles",
            ["source_cycle_id"],
            ["cycle_id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("risk_decisions") as batch:
        batch.add_column(sa.Column("source_cycle_id", sa.String(100)))
        batch.add_column(sa.Column("input_state_sequence", sa.Integer()))
        batch.create_foreign_key(
            "fk_risk_decision_source_cycle",
            "paper_cycles",
            ["source_cycle_id"],
            ["cycle_id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("order_intents") as batch:
        batch.add_column(sa.Column("source_cycle_id", sa.String(100)))
        batch.add_column(sa.Column("input_state_sequence", sa.Integer()))
        batch.add_column(sa.Column("symbol", sa.String(30)))
        batch.add_column(sa.Column("side", sa.String(10)))
        batch.add_column(sa.Column("quantity", sa.Numeric(38, 10)))
        batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("valid_until", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("decision_quote_id", sa.String(100)))
        batch.add_column(sa.Column("decision_reference_price", sa.Numeric(38, 12)))
        batch.create_foreign_key(
            "fk_order_intent_source_cycle",
            "paper_cycles",
            ["source_cycle_id"],
            ["cycle_id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("fills") as batch:
        batch.add_column(sa.Column("source_cycle_id", sa.String(100)))
        batch.add_column(sa.Column("quote_id", sa.String(100)))
        batch.add_column(sa.Column("quote_event_time", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("quote_available_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("symbol", sa.String(30)))
        batch.add_column(sa.Column("side", sa.String(10)))
        batch.add_column(sa.Column("quantity", sa.Numeric(38, 10)))
        batch.add_column(sa.Column("price", sa.Numeric(38, 12)))
        batch.add_column(sa.Column("commission_usd", sa.Numeric(38, 10)))
        batch.add_column(sa.Column("execution_scenario_id", sa.String(80)))
        batch.add_column(sa.Column("fill_hash", sa.String(64)))
        batch.create_foreign_key(
            "fk_fill_source_cycle",
            "paper_cycles",
            ["source_cycle_id"],
            ["cycle_id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_fill_order_quote_scenario",
            ["order_intent_id", "quote_id", "execution_scenario_id"],
        )
    with op.batch_alter_table("arm_state_snapshots") as batch:
        batch.add_column(sa.Column("source_cycle_id", sa.String(100)))
        batch.add_column(sa.Column("state_hash", sa.String(64)))
        batch.create_foreign_key(
            "fk_arm_state_source_cycle",
            "paper_cycles",
            ["source_cycle_id"],
            ["cycle_id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("nav_snapshots") as batch:
        batch.add_column(sa.Column("source_cycle_id", sa.String(100)))
        batch.add_column(sa.Column("quote_manifest_hash", sa.String(64)))
        batch.create_foreign_key(
            "fk_nav_source_cycle",
            "paper_cycles",
            ["source_cycle_id"],
            ["cycle_id"],
            ondelete="RESTRICT",
        )

    op.create_table(
        "paper_cycle_effects",
        sa.Column("effect_id", sa.String(100), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(100),
            sa.ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("effect_kind", sa.String(40), nullable=False),
        sa.Column("input_manifest_hash", sa.String(64), nullable=False),
        sa.Column("output_manifest_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "cycle_id",
            "effect_kind",
            name="uq_paper_cycle_effect_kind",
        ),
    )
    op.create_table(
        "paper_execution_attempts",
        sa.Column("attempt_id", sa.String(100), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(100),
            sa.ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "order_intent_id",
            sa.String(80),
            sa.ForeignKey("order_intents.order_intent_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quote_id", sa.String(100)),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("remaining_quantity_before", sa.Numeric(38, 10), nullable=False),
        sa.Column("remaining_quantity_after", sa.Numeric(38, 10), nullable=False),
        sa.Column("cumulative_notional_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("cumulative_commission_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("attempt_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "cycle_id",
            "order_intent_id",
            name="uq_execution_attempt_cycle_order",
        ),
    )
    op.create_table(
        "forward_strategy_candidates",
        sa.Column("candidate_id", sa.String(100), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_cycle_id",
            sa.String(100),
            sa.ForeignKey("paper_cycles.cycle_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("strategy_id", sa.String(40), nullable=False),
        sa.Column("strategy_version", sa.String(40), nullable=False),
        sa.Column("decision_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "data_available_cutoff",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason_code", sa.String(120), nullable=False),
        sa.Column("input_manifest_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "strategy_id",
            "strategy_version",
            "decision_time",
            name="uq_forward_candidate_identity",
        ),
    )
    op.create_table(
        "forecast_calibrations",
        sa.Column("calibration_id", sa.String(100), primary_key=True),
        sa.Column("strategy_id", sa.String(40), nullable=False),
        sa.Column("strategy_version", sa.String(40), nullable=False),
        sa.Column("feature_version", sa.String(80), nullable=False),
        sa.Column("horizon", sa.String(10), nullable=False),
        sa.Column("cost_model_version", sa.String(80), nullable=False),
        sa.Column("trained_through", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "strategy_promotion_decisions",
        sa.Column("promotion_id", sa.String(100), primary_key=True),
        sa.Column("strategy_id", sa.String(40), nullable=False),
        sa.Column("strategy_version", sa.String(40), nullable=False),
        sa.Column(
            "calibration_id",
            sa.String(100),
            sa.ForeignKey("forecast_calibrations.calibration_id", ondelete="RESTRICT"),
        ),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason_code", sa.String(120), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    )

    if dialect == "sqlite":
        _create_sqlite_guards(
            (*AFFECTED_APPEND_ONLY_TABLES, *NEW_APPEND_ONLY_TABLES, "arm_state_snapshots")
        )
    elif dialect == "postgresql":
        _create_postgres_guards((*NEW_APPEND_ONLY_TABLES, "arm_state_snapshots"))


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    guarded = (*AFFECTED_APPEND_ONLY_TABLES, *NEW_APPEND_ONLY_TABLES, "arm_state_snapshots")
    if dialect == "sqlite":
        _drop_sqlite_guards(guarded)
    elif dialect == "postgresql":
        _drop_postgres_guards((*NEW_APPEND_ONLY_TABLES, "arm_state_snapshots"))

    op.drop_table("strategy_promotion_decisions")
    op.drop_table("forecast_calibrations")
    op.drop_table("forward_strategy_candidates")
    op.drop_table("paper_execution_attempts")
    op.drop_table("paper_cycle_effects")
    with op.batch_alter_table("nav_snapshots") as batch:
        batch.drop_constraint("fk_nav_source_cycle", type_="foreignkey")
        batch.drop_column("quote_manifest_hash")
        batch.drop_column("source_cycle_id")
    with op.batch_alter_table("arm_state_snapshots") as batch:
        batch.drop_constraint("fk_arm_state_source_cycle", type_="foreignkey")
        batch.drop_column("state_hash")
        batch.drop_column("source_cycle_id")
    with op.batch_alter_table("fills") as batch:
        batch.drop_constraint("uq_fill_order_quote_scenario", type_="unique")
        batch.drop_constraint("fk_fill_source_cycle", type_="foreignkey")
        for column in (
            "fill_hash",
            "execution_scenario_id",
            "commission_usd",
            "price",
            "quantity",
            "side",
            "symbol",
            "quote_available_at",
            "quote_event_time",
            "quote_id",
            "source_cycle_id",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("order_intents") as batch:
        batch.drop_constraint("fk_order_intent_source_cycle", type_="foreignkey")
        for column in (
            "valid_until",
            "decision_reference_price",
            "decision_quote_id",
            "created_at",
            "quantity",
            "side",
            "symbol",
            "input_state_sequence",
            "source_cycle_id",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("risk_decisions") as batch:
        batch.drop_constraint("fk_risk_decision_source_cycle", type_="foreignkey")
        batch.drop_column("input_state_sequence")
        batch.drop_column("source_cycle_id")
    with op.batch_alter_table("portfolio_decisions") as batch:
        batch.drop_constraint("fk_portfolio_decision_source_cycle", type_="foreignkey")
        batch.drop_column("input_state_sequence")
        batch.drop_column("source_cycle_id")

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
