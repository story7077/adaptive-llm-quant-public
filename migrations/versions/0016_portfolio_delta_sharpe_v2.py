"""Add immutable whole-portfolio comparison contracts.

Revision ID: 0016_portfolio_delta_sharpe_v2
Revises: 0015_meta_controller_v1
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_portfolio_delta_sharpe_v2"
down_revision: str | None = "0015_meta_controller_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = ("portfolio_comparison_contracts",)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(
        "portfolio_comparison_contracts",
        sa.Column(
            "comparison_contract_id",
            sa.String(160),
            primary_key=True,
        ),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey(
                "challenger_manifests.challenger_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("candidate_artifact_hash", sa.String(64), nullable=False),
        sa.Column(
            "champion_portfolio_manifest_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column(
            "candidate_portfolio_manifest_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("allocation_policy_hash", sa.String(64), nullable=False),
        sa.Column(
            "weight_selection_data_cutoff",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "allocation_policy_created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("contract_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "weight_selection_data_cutoff <= allocation_policy_created_at "
            "AND allocation_policy_created_at <= created_at",
            name="ck_portfolio_comparison_pit",
        ),
        sa.UniqueConstraint(
            "challenger_id",
            "candidate_artifact_hash",
            name="uq_portfolio_comparison_challenger_artifact",
        ),
    )
    op.create_index(
        "ix_portfolio_comparison_challenger_created",
        "portfolio_comparison_contracts",
        ("challenger_id", "created_at", "comparison_contract_id"),
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
        "ix_portfolio_comparison_challenger_created",
        table_name="portfolio_comparison_contracts",
    )
    op.drop_table("portfolio_comparison_contracts")


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
