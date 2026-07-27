"""Add trusted promotion evidence and explicit Champion designations.

Revision ID: 0011_trusted_promotion_designation
Revises: 0010_oos_production_lockbox
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_trusted_promotion_designation"
down_revision: str | None = "0010_oos_production_lockbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "research_shadow_performance_summaries",
    "research_promotion_evidence",
    "trusted_promotion_evaluations",
    "research_champion_designations",
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        # Alembic creates version_num as VARCHAR(32) by default. This revision
        # identifier is longer than 32 characters, so widen the control column
        # before Alembic records the completed migration.
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(32),
            type_=sa.String(128),
            existing_nullable=False,
        )
    op.create_table(
        "research_shadow_performance_summaries",
        sa.Column("summary_id", sa.String(100), primary_key=True),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("shadow_pair_id", sa.String(100), nullable=False),
        sa.Column("run_id", sa.String(100), nullable=False),
        sa.Column("source_summary_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "materialized_evidence_hash",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("summary_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("common_sessions", sa.Integer(), nullable=False),
        sa.Column(
            "data_available_cutoff",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "common_sessions > 0",
            name="ck_research_shadow_summary_sessions",
        ),
        sa.CheckConstraint(
            "created_at >= data_available_cutoff",
            name="ck_research_shadow_summary_cutoff",
        ),
        sa.UniqueConstraint(
            "challenger_id",
            "materialized_evidence_hash",
            name="uq_research_shadow_summary_materialized",
        ),
    )
    op.create_index(
        "ix_research_shadow_summary_challenger_created",
        "research_shadow_performance_summaries",
        ("challenger_id", "created_at", "summary_id"),
    )
    op.create_table(
        "research_promotion_evidence",
        sa.Column("evidence_id", sa.String(100), primary_key=True),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "shadow_summary_id",
            sa.String(100),
            sa.ForeignKey(
                "research_shadow_performance_summaries.summary_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("evidence_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_research_promotion_evidence_challenger_created",
        "research_promotion_evidence",
        ("challenger_id", "created_at", "evidence_id"),
    )
    op.create_table(
        "trusted_promotion_evaluations",
        sa.Column("evaluation_id", sa.String(100), primary_key=True),
        sa.Column(
            "evidence_id",
            sa.String(100),
            sa.ForeignKey("research_promotion_evidence.evidence_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "promotion_decision_id",
            sa.String(100),
            sa.ForeignKey(
                "research_promotion_decisions.promotion_decision_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("contract_hash", sa.String(64), nullable=False),
        sa.Column("verdict", sa.String(50), nullable=False),
        sa.Column("evaluation_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "verdict IN ('INELIGIBLE','ELIGIBLE_REQUIRES_MANUAL_APPROVAL')",
            name="ck_trusted_promotion_evaluation_verdict",
        ),
        sa.UniqueConstraint(
            "evidence_hash",
            "contract_hash",
            name="uq_trusted_promotion_evidence_contract",
        ),
    )
    op.create_index(
        "ix_trusted_promotion_challenger_created",
        "trusted_promotion_evaluations",
        ("challenger_id", "created_at", "evaluation_id"),
    )
    op.create_table(
        "research_champion_designations",
        sa.Column("designation_id", sa.String(100), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False, unique=True),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(80), nullable=False),
        sa.Column("candidate_artifact_hash", sa.String(64), nullable=False),
        sa.Column(
            "source_challenger_id",
            sa.String(100),
            sa.ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "trusted_evaluation_id",
            sa.String(100),
            sa.ForeignKey(
                "trusted_promotion_evaluations.evaluation_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "manual_approval_decision_id",
            sa.String(100),
            sa.ForeignKey(
                "research_promotion_decisions.promotion_decision_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "previous_designation_id",
            sa.String(100),
            sa.ForeignKey(
                "research_champion_designations.designation_id",
                ondelete="RESTRICT",
            ),
        ),
        sa.Column("expected_current_version", sa.String(80), nullable=False),
        sa.Column("designated_by", sa.String(120), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column(
            "automatic_promotion_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "real_order_routing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("designation_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("designated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_research_champion_designation_sequence",
        ),
        sa.CheckConstraint(
            "automatic_promotion_enabled = false",
            name="ck_research_champion_no_auto",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_research_champion_no_real_routing",
        ),
        sa.UniqueConstraint(
            "strategy_id",
            "strategy_version",
            name="uq_research_champion_strategy_version",
        ),
    )
    op.create_index(
        "ix_research_champion_designation_latest",
        "research_champion_designations",
        ("sequence", "designated_at", "designation_id"),
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
        "ix_research_champion_designation_latest",
        table_name="research_champion_designations",
    )
    op.drop_table("research_champion_designations")
    op.drop_index(
        "ix_trusted_promotion_challenger_created",
        table_name="trusted_promotion_evaluations",
    )
    op.drop_table("trusted_promotion_evaluations")
    op.drop_index(
        "ix_research_promotion_evidence_challenger_created",
        table_name="research_promotion_evidence",
    )
    op.drop_table("research_promotion_evidence")
    op.drop_index(
        "ix_research_shadow_summary_challenger_created",
        table_name="research_shadow_performance_summaries",
    )
    op.drop_table("research_shadow_performance_summaries")


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
