"""Add append-only prospective Candidate request and execution evidence.

Revision ID: 0018_candidate_prospective_v1
Revises: 0017_chronological_meta_oos_v1
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_candidate_prospective_v1"
down_revision: str | None = "0017_chronological_meta_oos_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUEST_TABLE = "research_candidate_prospective_requests"
EXECUTION_TABLE = "research_candidate_prospective_executions"
APPEND_ONLY_TABLES = (REQUEST_TABLE, EXECUTION_TABLE)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(
        REQUEST_TABLE,
        sa.Column("prospective_request_id", sa.String(160), primary_key=True),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey(
                "challenger_manifests.challenger_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "candidate_artifact_bundle_id",
            sa.String(160),
            sa.ForeignKey(
                "research_candidate_artifacts.bundle_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("candidate_artifact_hash", sa.String(64), nullable=False),
        sa.Column("candidate_config_hash", sa.String(64), nullable=False),
        sa.Column("strategy_config_content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "parent_run_id",
            sa.String(100),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "parent_portfolio_decision_id",
            sa.String(80),
            sa.ForeignKey(
                "portfolio_decisions.portfolio_decision_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
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
            "evaluation_anchor_id",
            sa.String(100),
            sa.ForeignKey(
                "strategy_evaluation_anchors.evaluation_anchor_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "prior_prospective_request_id",
            sa.String(160),
            sa.ForeignKey(
                f"{REQUEST_TABLE}.prospective_request_id",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
        sa.Column(
            "parent_scheduled_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "signal_data_cutoff",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("request_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "source_manifest_hash",
            sa.String(64),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "host_config_manifest_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("evidence_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "real_order_routing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("source_manifest_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "challenger_id",
            "parent_portfolio_decision_id",
            name="uq_candidate_prospective_parent_decision",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_candidate_prospective_request_paper_only",
        ),
    )
    op.create_index(
        "ix_candidate_prospective_request_schedule",
        REQUEST_TABLE,
        ("challenger_id", "parent_scheduled_at"),
    )
    op.create_table(
        EXECUTION_TABLE,
        sa.Column("execution_id", sa.String(160), primary_key=True),
        sa.Column(
            "prospective_request_id",
            sa.String(160),
            sa.ForeignKey(
                f"{REQUEST_TABLE}.prospective_request_id",
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
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "runtime_attestation_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column(
            "security_contract_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("primary_response_hash", sa.String(64), nullable=True),
        sa.Column("replay_response_hash", sa.String(64), nullable=True),
        sa.Column("deterministic_match", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(160), nullable=True),
        sa.Column("success_identity", sa.String(160), nullable=True, unique=True),
        sa.Column("execution_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "real_order_routing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('SUCCEEDED', 'FAILED')",
            name="ck_candidate_prospective_execution_status",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_candidate_prospective_execution_paper_only",
        ),
    )
    op.create_index(
        "ix_candidate_prospective_execution_request",
        EXECUTION_TABLE,
        ("prospective_request_id", "status"),
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
        "ix_candidate_prospective_execution_request",
        table_name=EXECUTION_TABLE,
    )
    op.drop_table(EXECUTION_TABLE)
    op.drop_index(
        "ix_candidate_prospective_request_schedule",
        table_name=REQUEST_TABLE,
    )
    op.drop_table(REQUEST_TABLE)


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
