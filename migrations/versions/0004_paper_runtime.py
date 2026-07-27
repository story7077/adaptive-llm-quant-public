"""Versioned paper account bootstrap and runtime projections.

Revision ID: 0004_paper_runtime
Revises: 0003_live_market_data
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_paper_runtime"
down_revision: str | None = "0003_live_market_data"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "paper_account_specs",
    "paper_cash_balances",
    "paper_positions",
    "paper_bootstrap_marks",
    "paper_bootstrap_completions",
)


def upgrade() -> None:
    op.create_table(
        "paper_account_specs",
        sa.Column("account_spec_id", sa.String(80), primary_key=True),
        sa.Column("account_id", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(40), nullable=False),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "version",
            name="uq_paper_account_spec_version",
        ),
    )
    op.create_table(
        "paper_cash_balances",
        sa.Column("cash_balance_id", sa.String(80), primary_key=True),
        sa.Column(
            "account_spec_id",
            sa.String(80),
            sa.ForeignKey("paper_account_specs.account_spec_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount", sa.Numeric(38, 10), nullable=False),
        sa.Column("tradable", sa.Boolean(), nullable=False),
        sa.Column("exclusion_reason", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "account_spec_id",
            "currency",
            name="uq_paper_cash_spec_currency",
        ),
    )
    op.create_table(
        "paper_positions",
        sa.Column("paper_position_id", sa.String(80), primary_key=True),
        sa.Column(
            "account_spec_id",
            sa.String(80),
            sa.ForeignKey("paper_account_specs.account_spec_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 10), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "account_spec_id",
            "symbol",
            name="uq_paper_position_spec_symbol",
        ),
    )
    op.create_table(
        "paper_bootstrap_marks",
        sa.Column("bootstrap_mark_id", sa.String(80), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_spec_id",
            sa.String(80),
            sa.ForeignKey("paper_account_specs.account_spec_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("price", sa.Numeric(38, 12), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("marked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_kind", sa.String(40), nullable=False),
        sa.Column("source_record_id", sa.String(100)),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "symbol",
            name="uq_paper_bootstrap_run_symbol",
        ),
    )
    op.create_index(
        "ix_paper_bootstrap_marks_run_time",
        "paper_bootstrap_marks",
        ("run_id", "marked_at"),
    )
    op.create_table(
        "paper_bootstrap_completions",
        sa.Column("bootstrap_completion_id", sa.String(80), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "account_spec_id",
            sa.String(80),
            sa.ForeignKey("paper_account_specs.account_spec_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("common_mark_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("initial_nav_usd", sa.Numeric(38, 10), nullable=False),
        sa.Column("input_manifest_hash", sa.String(64), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "paper_cycles",
        sa.Column("cycle_id", sa.String(100), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("cycle_kind", sa.String(40), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_available_cutoff", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("lease_owner", sa.String(100)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_manifest_hash", sa.String(64)),
        sa.Column("output_manifest_hash", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("last_error_detail", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "cycle_kind",
            "scheduled_at",
            name="uq_paper_cycle_schedule",
        ),
    )
    op.create_index(
        "ix_paper_cycles_runnable",
        "paper_cycles",
        ("status", "scheduled_at", "lease_expires_at"),
    )
    op.create_table(
        "paper_runtime_status",
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column(
            "current_cycle_id",
            sa.String(100),
            sa.ForeignKey("paper_cycles.cycle_id", ondelete="SET NULL"),
        ),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("last_completed_cycle_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("last_error_detail", sa.String(500)),
        sa.Column("process_id", sa.Integer()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    _create_append_only_guards()


def downgrade() -> None:
    _drop_append_only_guards()
    op.drop_table("paper_runtime_status")
    op.drop_index("ix_paper_cycles_runnable", table_name="paper_cycles")
    op.drop_table("paper_cycles")
    op.drop_table("paper_bootstrap_completions")
    op.drop_index(
        "ix_paper_bootstrap_marks_run_time",
        table_name="paper_bootstrap_marks",
    )
    op.drop_table("paper_bootstrap_marks")
    op.drop_table("paper_positions")
    op.drop_table("paper_cash_balances")
    op.drop_table("paper_account_specs")


def _create_append_only_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
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


def _drop_append_only_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for table in APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    elif dialect == "sqlite":
        for table in APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
