"""Versioned adaptive control plane.

Revision ID: 0002_adaptive_control_plane
Revises: 0001_phase0
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_adaptive_control_plane"
down_revision: str | None = "0001_phase0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_APPEND_ONLY_TABLES = (
    "policy_versions",
    "commander_selections",
    "commander_requests",
    "commander_decisions",
    "commander_decision_results",
)


def upgrade() -> None:
    op.create_table(
        "commander_selections",
        sa.Column("selection_id", sa.String(80), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, unique=True),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("reasoning_profile", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
    )
    op.create_table(
        "commander_requests",
        sa.Column("request_id", sa.String(100), primary_key=True),
        sa.Column(
            "selection_id",
            sa.String(80),
            sa.ForeignKey("commander_selections.selection_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("selection_version", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("arm_scope", sa.String(30), nullable=False),
        sa.Column("base_policy_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("context_manifest_hash", sa.String(64), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "commander_decisions",
        sa.Column("decision_id", sa.String(100), primary_key=True),
        sa.Column(
            "request_id",
            sa.String(100),
            sa.ForeignKey("commander_requests.request_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("request_id", name="uq_commander_decision_request"),
    )
    op.create_table(
        "commander_decision_results",
        sa.Column("result_id", sa.String(100), primary_key=True),
        sa.Column(
            "decision_id",
            sa.String(100),
            sa.ForeignKey("commander_decisions.decision_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("reason_detail", sa.String(500), nullable=False),
        sa.Column("arm_scope", sa.String(30), nullable=False),
        sa.Column("base_policy_version", sa.Integer(), nullable=False),
        sa.Column("applied_policy_version", sa.Integer()),
        sa.Column("compiled_policy_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
    )
    _create_append_only_guards()


def downgrade() -> None:
    _drop_append_only_guards()
    op.drop_table("commander_decision_results")
    op.drop_table("commander_decisions")
    op.drop_table("commander_requests")
    op.drop_table("commander_selections")


def _create_append_only_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for table in NEW_APPEND_ONLY_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()
                """
            )
    elif dialect == "sqlite":
        for table in NEW_APPEND_ONLY_TABLES:
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
        for table in NEW_APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    elif dialect == "sqlite":
        for table in NEW_APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
