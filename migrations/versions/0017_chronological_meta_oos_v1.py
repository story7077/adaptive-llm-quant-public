"""Add isolated append-only chronological meta-OOS records.

Revision ID: 0017_chronological_meta_oos_v1
Revises: 0016_portfolio_delta_sharpe_v2
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_chronological_meta_oos_v1"
down_revision: str | None = "0016_portfolio_delta_sharpe_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "chronological_meta_oos_plans",
    "meta_oos_outer_audit_reservations",
    "meta_oos_epoch_arm_audit_records",
    "chronological_meta_oos_results",
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(
        "chronological_meta_oos_plans",
        sa.Column("plan_id", sa.String(160), primary_key=True),
        sa.Column("plan_version", sa.String(160), nullable=False),
        sa.Column(
            "initial_champion_manifest_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column(
            "evaluation_contract_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("outer_audit_dataset_id", sa.String(160), nullable=False),
        sa.Column(
            "outer_audit_budget_ordinal",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column("plan_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "real_order_routing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "outer_audit_dataset_id",
            "outer_audit_budget_ordinal",
            name="uq_meta_oos_plan_dataset_budget",
        ),
        sa.CheckConstraint(
            "outer_audit_budget_ordinal >= 1",
            name="ck_meta_oos_plan_budget_positive",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_meta_oos_plan_paper_only",
        ),
    )
    op.create_table(
        "meta_oos_outer_audit_reservations",
        sa.Column("reservation_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey(
                "chronological_meta_oos_plans.plan_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
            unique=True,
        ),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("outer_audit_dataset_id", sa.String(160), nullable=False),
        sa.Column(
            "outer_audit_budget_ordinal",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column(
            "reservation_hash",
            sa.String(64),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "real_order_routing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "outer_audit_dataset_id",
            "outer_audit_budget_ordinal",
            name="uq_meta_oos_reservation_dataset_budget",
        ),
        sa.UniqueConstraint(
            "outer_audit_dataset_id",
            "idempotency_key",
            name="uq_meta_oos_reservation_idempotency",
        ),
        sa.CheckConstraint(
            "outer_audit_budget_ordinal >= 1",
            name="ck_meta_oos_reservation_budget_positive",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_meta_oos_reservation_expiry",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_meta_oos_reservation_paper_only",
        ),
    )
    op.create_table(
        "meta_oos_epoch_arm_audit_records",
        sa.Column("record_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey(
                "chronological_meta_oos_plans.plan_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("epoch_id", sa.String(160), nullable=False),
        sa.Column("arm", sa.String(50), nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False),
        sa.Column("memory_snapshot_hash", sa.String(64), nullable=True),
        sa.Column("private_outcome_hash", sa.String(64), nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "real_order_routing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "plan_id",
            "epoch_id",
            "arm",
            name="uq_meta_oos_epoch_arm_record",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_meta_oos_epoch_arm_paper_only",
        ),
    )
    op.create_index(
        "ix_meta_oos_epoch_arm_plan",
        "meta_oos_epoch_arm_audit_records",
        ("plan_id", "epoch_id", "arm"),
    )
    op.create_table(
        "chronological_meta_oos_results",
        sa.Column("result_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey(
                "chronological_meta_oos_plans.plan_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "reservation_id",
            sa.String(160),
            sa.ForeignKey(
                "meta_oos_outer_audit_reservations.reservation_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "evaluation_contract_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("adaptive_system_pass", sa.Boolean(), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "real_order_routing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_meta_oos_result_paper_only",
        ),
    )
    if dialect == "sqlite":
        _create_sqlite_guards(APPEND_ONLY_TABLES)
    elif dialect == "postgresql":
        _create_postgres_guards(APPEND_ONLY_TABLES)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _drop_sqlite_guards(APPEND_ONLY_TABLES)
    elif dialect == "postgresql":
        _drop_postgres_guards(APPEND_ONLY_TABLES)
    op.drop_table("chronological_meta_oos_results")
    op.drop_index(
        "ix_meta_oos_epoch_arm_plan",
        table_name="meta_oos_epoch_arm_audit_records",
    )
    op.drop_table("meta_oos_epoch_arm_audit_records")
    op.drop_table("meta_oos_outer_audit_reservations")
    op.drop_table("chronological_meta_oos_plans")


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


def _drop_sqlite_guards(tables: Sequence[str]) -> None:
    for table in tables:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")


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
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}"
        )
