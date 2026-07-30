"""Add completed-session operator deep Research work.

Revision ID: 0022_operator_deep_research_work
Revises: 0021_research_execution_lease_v1
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_operator_deep_research_work"
down_revision: str | None = "0021_research_execution_lease_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "research_schedule_work_items"
CONSTRAINT = "ck_research_schedule_work_kind"
OLD_WORK_KINDS = (
    "DAILY_AGGREGATION",
    "WEEKLY_DEEP_RESEARCH",
    "EVIDENCE_TRIGGERED_RESEARCH",
    "OUTCOME_MATURATION",
    "RESEARCH_MEMORY_MATERIALIZATION",
)
NEW_WORK_KINDS = (
    *OLD_WORK_KINDS,
    "OPERATOR_DEEP_RESEARCH",
)


def upgrade() -> None:
    _replace_work_kind_constraint(NEW_WORK_KINDS)


def downgrade() -> None:
    incompatible_count = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM research_schedule_work_items "
            "WHERE work_kind = 'OPERATOR_DEEP_RESEARCH'"
        )
    ).scalar_one()
    if incompatible_count:
        raise RuntimeError(
            "cannot downgrade while operator deep research work exists"
        )
    _replace_work_kind_constraint(OLD_WORK_KINDS)


def _replace_work_kind_constraint(work_kinds: Sequence[str]) -> None:
    dialect = op.get_bind().dialect.name
    expression = "work_kind IN (" + ",".join(
        f"'{value}'" for value in work_kinds
    ) + ")"
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_no_delete")
        with op.batch_alter_table(
            TABLE,
            recreate="always",
        ) as batch:
            batch.drop_constraint(
                CONSTRAINT,
                type_="check",
            )
            batch.create_check_constraint(
                CONSTRAINT,
                expression,
            )
        _create_sqlite_guards()
    elif dialect == "postgresql":
        op.drop_constraint(
            CONSTRAINT,
            TABLE,
            type_="check",
        )
        op.create_check_constraint(
            CONSTRAINT,
            TABLE,
            expression,
        )


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
