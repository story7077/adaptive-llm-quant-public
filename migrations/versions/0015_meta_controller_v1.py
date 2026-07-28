"""Add immutable Meta Controller plans and V2 proposals.

Revision ID: 0015_meta_controller_v1
Revises: 0014_experiment_outcome_ledger
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_meta_controller_v1"
down_revision: str | None = "0014_experiment_outcome_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLAN_TABLE = "research_action_plans"
PROPOSAL_TABLE = "algorithm_proposals_v2"
APPEND_ONLY_TABLES = (PLAN_TABLE, PROPOSAL_TABLE)


def upgrade() -> None:
    op.create_table(
        PLAN_TABLE,
        sa.Column("action_plan_id", sa.String(160), primary_key=True),
        sa.Column("research_cycle_id", sa.String(160), nullable=False),
        sa.Column("policy_version", sa.String(160), nullable=False),
        sa.Column(
            "research_memory_snapshot_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("training_view_hash", sa.String(64), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "research_cycle_id",
            name="uq_research_action_plan_cycle",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_research_action_plan_idempotency",
        ),
    )
    op.create_index(
        "ix_research_action_plan_memory_context",
        PLAN_TABLE,
        (
            "research_memory_snapshot_hash",
            "context_hash",
            "generated_at",
        ),
    )

    op.create_table(
        PROPOSAL_TABLE,
        sa.Column("proposal_id", sa.String(160), primary_key=True),
        sa.Column("research_cycle_id", sa.String(160), nullable=False),
        sa.Column("hypothesis_id", sa.String(160), nullable=False),
        sa.Column("parent_strategy_id", sa.String(160), nullable=False),
        sa.Column("parent_strategy_version", sa.String(80), nullable=False),
        sa.Column("proposed_strategy_id", sa.String(160), nullable=False),
        sa.Column("proposed_strategy_version", sa.String(80), nullable=False),
        sa.Column("primary_action_kind", sa.String(50), nullable=False),
        sa.Column("action_plan_hash", sa.String(64), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_algorithm_proposal_v2_cycle_action",
        PROPOSAL_TABLE,
        ("research_cycle_id", "primary_action_kind", "created_at"),
    )

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _create_sqlite_guards()
    elif dialect == "postgresql":
        _create_postgres_guards()


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _drop_sqlite_guards()
    elif dialect == "postgresql":
        _drop_postgres_guards()
    op.drop_index(
        "ix_algorithm_proposal_v2_cycle_action",
        table_name=PROPOSAL_TABLE,
    )
    op.drop_table(PROPOSAL_TABLE)
    op.drop_index(
        "ix_research_action_plan_memory_context",
        table_name=PLAN_TABLE,
    )
    op.drop_table(PLAN_TABLE)


def _create_sqlite_guards() -> None:
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


def _drop_sqlite_guards() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")


def _create_postgres_guards() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()
            """
        )


def _drop_postgres_guards() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}"
        )
