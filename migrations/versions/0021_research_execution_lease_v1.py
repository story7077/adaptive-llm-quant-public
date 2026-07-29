"""Add fenced execution leases to Research Scheduler events.

Revision ID: 0021_research_execution_lease_v1
Revises: 0020_candidate_evaluation_dataset_v2
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_research_execution_lease_v1"
down_revision: str | None = "0020_candidate_evaluation_dataset_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "research_schedule_events"
CONSTRAINT = "ck_research_schedule_event_type"
OLD_EVENT_TYPES = (
    "PLANNED",
    "LEASE_ACQUIRED",
    "LEASE_RECLAIMED",
    "DISPATCHED",
    "SUCCEEDED",
    "FAILED",
)
NEW_EVENT_TYPES = (
    *OLD_EVENT_TYPES[:4],
    "EXECUTION_LEASE_ACQUIRED",
    "EXECUTION_LEASE_RECLAIMED",
    "EXECUTION_LEASE_RENEWED",
    *OLD_EVENT_TYPES[4:],
)


def upgrade() -> None:
    _replace_event_type_constraint(NEW_EVENT_TYPES)


def downgrade() -> None:
    incompatible_count = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM research_schedule_events "
            "WHERE event_type IN "
            "('EXECUTION_LEASE_ACQUIRED',"
            "'EXECUTION_LEASE_RECLAIMED',"
            "'EXECUTION_LEASE_RENEWED')"
        )
    ).scalar_one()
    if incompatible_count:
        raise RuntimeError(
            "cannot downgrade while research execution lease events exist"
        )
    _replace_event_type_constraint(OLD_EVENT_TYPES)


def _replace_event_type_constraint(event_types: Sequence[str]) -> None:
    dialect = op.get_bind().dialect.name
    expression = "event_type IN (" + ",".join(
        f"'{value}'" for value in event_types
    ) + ")"
    if dialect == "sqlite":
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{TABLE}_no_update"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{TABLE}_no_delete"
        )
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
