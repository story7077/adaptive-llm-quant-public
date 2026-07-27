"""Add append-only production OOS budget reservations.

Revision ID: 0010_oos_production_lockbox
Revises: 0009_research_plane_v1
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_oos_production_lockbox"
down_revision: str | None = "0009_research_plane_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = ("oos_budget_reservations",)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(
        "oos_budget_reservations",
        sa.Column("reservation_id", sa.String(100), primary_key=True),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("experiment_family", sa.String(100), nullable=False),
        sa.Column("submission_number", sa.Integer(), nullable=False),
        sa.Column("submission_ordinal", sa.Integer(), nullable=False),
        sa.Column("oos_budget_ordinal", sa.Integer(), nullable=False),
        sa.Column("candidate_artifact_hash", sa.String(64), nullable=False),
        sa.Column("evaluation_contract_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("reservation_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "submission_number >= 1 AND submission_ordinal >= 1 "
            "AND oos_budget_ordinal >= 1",
            name="ck_oos_budget_reservation_ordinals",
        ),
        sa.CheckConstraint(
            "submission_number = submission_ordinal",
            name="ck_oos_budget_reservation_submission_sequence",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_oos_budget_reservation_expiry",
        ),
        sa.UniqueConstraint(
            "experiment_family",
            "idempotency_key",
            name="uq_oos_budget_reservation_idempotency",
        ),
        sa.UniqueConstraint(
            "experiment_family",
            "submission_number",
            name="uq_oos_budget_reservation_submission",
        ),
        sa.UniqueConstraint(
            "experiment_family",
            "submission_ordinal",
            name="uq_oos_budget_reservation_submission_ordinal",
        ),
        sa.UniqueConstraint(
            "experiment_family",
            "oos_budget_ordinal",
            name="uq_oos_budget_reservation_budget_ordinal",
        ),
    )
    op.create_index(
        "ix_oos_budget_reservation_family_created",
        "oos_budget_reservations",
        ("experiment_family", "created_at", "reservation_id"),
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

    op.drop_index(
        "ix_oos_budget_reservation_family_created",
        table_name="oos_budget_reservations",
    )
    op.drop_table("oos_budget_reservations")


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
