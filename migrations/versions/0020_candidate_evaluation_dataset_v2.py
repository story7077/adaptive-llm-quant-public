"""Add append-only multi-cutoff Candidate evaluation artifacts.

Revision ID: 0020_candidate_evaluation_dataset_v2
Revises: 0019_candidate_prospective_outcomes_v1
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_candidate_evaluation_dataset_v2"
down_revision: str | None = "0019_candidate_prospective_outcomes_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATASET_TABLE = "research_candidate_evaluation_datasets_v2"
TRACE_TABLE = "research_candidate_evaluation_traces_v2"
TABLES = (DATASET_TABLE, TRACE_TABLE)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(
        DATASET_TABLE,
        sa.Column("dataset_id", sa.String(160), primary_key=True),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey(
                "challenger_manifests.challenger_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "candidate_artifact_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column(
            "source_manifest_hash",
            sa.String(64),
            nullable=False,
            unique=True,
        ),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("base_session_count", sa.Integer(), nullable=False),
        sa.Column("scenario_count", sa.Integer(), nullable=False),
        sa.Column(
            "dataset_hash",
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
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "base_session_count >= 126",
            name="ck_candidate_evaluation_dataset_v2_sessions",
        ),
        sa.CheckConstraint(
            "scenario_count >= base_session_count",
            name="ck_candidate_evaluation_dataset_v2_scenarios",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_candidate_evaluation_dataset_v2_paper_only",
        ),
    )
    op.create_index(
        "ix_candidate_evaluation_dataset_v2_source",
        DATASET_TABLE,
        ("challenger_id", "source_manifest_hash"),
    )
    op.create_table(
        TRACE_TABLE,
        sa.Column("trace_id", sa.String(160), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.String(160),
            sa.ForeignKey(
                f"{DATASET_TABLE}.dataset_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey(
                "challenger_manifests.challenger_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "candidate_artifact_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column(
            "source_manifest_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column(
            "evaluation_contract_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column(
            "replay_artifact_hash",
            sa.String(64),
            sa.ForeignKey(
                "research_replay_artifacts.artifact_hash",
                ondelete="RESTRICT",
            ),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "trace_hash",
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
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_candidate_evaluation_trace_v2_paper_only",
        ),
    )
    op.create_index(
        "ix_candidate_evaluation_trace_v2_binding",
        TRACE_TABLE,
        ("challenger_id", "source_manifest_hash"),
    )
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
        "ix_candidate_evaluation_trace_v2_binding",
        table_name=TRACE_TABLE,
    )
    op.drop_table(TRACE_TABLE)
    op.drop_index(
        "ix_candidate_evaluation_dataset_v2_source",
        table_name=DATASET_TABLE,
    )
    op.drop_table(DATASET_TABLE)


def _create_sqlite_guards() -> None:
    for table in TABLES:
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
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")


def _create_postgres_guards() -> None:
    for table in TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()
            """
        )


def _drop_postgres_guards() -> None:
    for table in TABLES:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}"
        )
