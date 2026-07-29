"""Add append-only prospective Candidate forward outcomes.

Revision ID: 0019_candidate_prospective_outcomes_v1
Revises: 0018_candidate_prospective_v1
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019_candidate_prospective_outcomes_v1"
down_revision: str | None = "0018_candidate_prospective_v1"
branch_labels: str | None = None
depends_on: str | None = None

OUTCOME_TABLE = "research_candidate_prospective_outcomes"
FAILURE_TABLE = "research_candidate_prospective_outcome_failures"
TABLES = (OUTCOME_TABLE, FAILURE_TABLE)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(
        OUTCOME_TABLE,
        sa.Column("outcome_id", sa.String(160), primary_key=True),
        sa.Column(
            "prospective_request_id",
            sa.String(160),
            sa.ForeignKey(
                "research_candidate_prospective_requests.prospective_request_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            sa.String(160),
            sa.ForeignKey(
                "research_candidate_prospective_executions.execution_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
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
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("execution_hash", sa.String(64), nullable=False),
        sa.Column(
            "decision_calendar_session_id",
            sa.String(100),
            sa.ForeignKey(
                "market_calendar_sessions.calendar_session_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "implementation_calendar_session_id",
            sa.String(100),
            sa.ForeignKey(
                "market_calendar_sessions.calendar_session_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "evaluation_calendar_session_id",
            sa.String(100),
            sa.ForeignKey(
                "market_calendar_sessions.calendar_session_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("calendar_version", sa.String(80), nullable=False),
        sa.Column("market_dataset_version", sa.String(120), nullable=False),
        sa.Column(
            "decision_time",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "outcome_data_cutoff",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "outcome_available_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column(
            "source_manifest_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("cost_model_hash", sa.String(64), nullable=False),
        sa.Column(
            "outcome_hash",
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
        sa.UniqueConstraint(
            "prospective_request_id",
            name="uq_candidate_prospective_outcome_request",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_candidate_prospective_outcome_paper_only",
        ),
    )
    op.create_index(
        "ix_candidate_prospective_outcome_maturity",
        OUTCOME_TABLE,
        ("challenger_id", "outcome_available_at"),
    )
    op.create_table(
        FAILURE_TABLE,
        sa.Column("failure_id", sa.String(160), primary_key=True),
        sa.Column(
            "prospective_request_id",
            sa.String(160),
            sa.ForeignKey(
                "research_candidate_prospective_requests.prospective_request_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            sa.String(160),
            sa.ForeignKey(
                "research_candidate_prospective_executions.execution_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
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
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("execution_hash", sa.String(64), nullable=False),
        sa.Column(
            "implementation_calendar_session_id",
            sa.String(100),
            sa.ForeignKey(
                "market_calendar_sessions.calendar_session_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "evaluation_calendar_session_id",
            sa.String(100),
            sa.ForeignKey(
                "market_calendar_sessions.calendar_session_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "outcome_data_cutoff",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(160), nullable=False),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("failure_hash", sa.String(64), nullable=False, unique=True),
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
        sa.UniqueConstraint(
            "prospective_request_id",
            name="uq_candidate_prospective_outcome_failure_request",
        ),
        sa.CheckConstraint(
            "error_code = 'PROSPECTIVE_OUTCOME_DATA_WINDOW_MISSED'",
            name="ck_candidate_prospective_outcome_failure_code",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_candidate_prospective_outcome_failure_paper_only",
        ),
    )
    op.create_index(
        "ix_candidate_prospective_outcome_failure_challenger",
        FAILURE_TABLE,
        ("challenger_id", "outcome_data_cutoff"),
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
        "ix_candidate_prospective_outcome_failure_challenger",
        table_name=FAILURE_TABLE,
    )
    op.drop_table(FAILURE_TABLE)
    op.drop_index(
        "ix_candidate_prospective_outcome_maturity",
        table_name=OUTCOME_TABLE,
    )
    op.drop_table(OUTCOME_TABLE)


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
