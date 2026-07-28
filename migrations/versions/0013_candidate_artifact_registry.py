"""Add the immutable trusted Candidate artifact registry.

Revision ID: 0013_candidate_artifact_registry
Revises: 0012_research_scheduler_v1
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_candidate_artifact_registry"
down_revision: str | None = "0012_research_scheduler_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "research_candidate_artifacts"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(
        TABLE,
        sa.Column("bundle_id", sa.String(160), primary_key=True),
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
            "proposal_id",
            sa.String(100),
            sa.ForeignKey(
                "algorithm_proposals.proposal_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "research_cycle_id",
            sa.String(100),
            sa.ForeignKey(
                "research_cycles.research_cycle_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("candidate_tree_hash", sa.String(64), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("test_manifest_hash", sa.String(64), nullable=False),
        sa.Column("declared_entrypoint", sa.String(512), nullable=False),
        sa.Column(
            "bundle_hash",
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
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_research_candidate_artifact_paper_only",
        ),
    )
    op.create_index(
        "ix_research_candidate_artifact_cycle",
        TABLE,
        ("research_cycle_id", "challenger_id"),
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
        "ix_research_candidate_artifact_cycle",
        table_name=TABLE,
    )
    op.drop_table(TABLE)


def _create_sqlite_guards() -> None:
    op.execute(
        f"""
        CREATE TRIGGER trg_{TABLE}_no_update
        BEFORE UPDATE ON {TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'append-only table cannot be updated');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{TABLE}_no_delete
        BEFORE DELETE ON {TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'append-only table cannot be deleted');
        END
        """
    )


def _drop_sqlite_guards() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_no_update")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_no_delete")


def _create_postgres_guards() -> None:
    op.execute(
        f"""
        CREATE TRIGGER trg_{TABLE}_append_only
        BEFORE UPDATE OR DELETE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()
        """
    )


def _drop_postgres_guards() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_append_only ON {TABLE}")
