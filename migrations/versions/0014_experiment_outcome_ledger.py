"""Add the immutable recursive-research experiment outcome ledger.

Revision ID: 0014_experiment_outcome_ledger
Revises: 0013_candidate_artifact_registry
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_experiment_outcome_ledger"
down_revision: str | None = "0013_candidate_artifact_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTION_TABLE = "research_experiment_actions"
EVENT_TABLE = "research_experiment_outcome_events"
SNAPSHOT_TABLE = "research_memory_snapshots"
APPEND_ONLY_TABLES = (ACTION_TABLE, EVENT_TABLE, SNAPSHOT_TABLE)
SCHEDULER_WORK_TABLE = "research_schedule_work_items"
SCHEDULER_WORK_KIND_CONSTRAINT = "ck_research_schedule_work_kind"
DISPATCH_TABLE = "research_work_dispatch_receipts"
DISPATCH_TARGET_CONSTRAINT = "ck_research_dispatch_target"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    _replace_scheduler_work_kind_constraint(
        dialect=dialect,
        include_recursive_kinds=True,
    )
    _replace_dispatch_target_constraint(
        dialect=dialect,
        include_recursive_targets=True,
    )
    op.create_table(
        ACTION_TABLE,
        sa.Column("action_id", sa.String(160), primary_key=True),
        sa.Column("experiment_id", sa.String(160), nullable=False),
        sa.Column("research_cycle_id", sa.String(160), nullable=False),
        sa.Column("proposal_id", sa.String(160), nullable=False),
        sa.Column("challenger_id", sa.String(160), nullable=False),
        sa.Column("information_role", sa.String(40), nullable=False),
        sa.Column("primary_action_kind", sa.String(50), nullable=False),
        sa.Column(
            "maturity_due_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "meta_training_permitted",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("action_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "experiment_id",
            name="uq_research_experiment_action_experiment",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_research_experiment_action_idempotency",
        ),
    )
    op.create_index(
        "ix_research_experiment_action_lookup",
        ACTION_TABLE,
        (
            "challenger_id",
            "information_role",
            "primary_action_kind",
            "maturity_due_at",
        ),
    )

    op.create_table(
        EVENT_TABLE,
        sa.Column("event_id", sa.String(160), primary_key=True),
        sa.Column(
            "action_id",
            sa.String(160),
            sa.ForeignKey(
                f"{ACTION_TABLE}.action_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("experiment_id", sa.String(160), nullable=False),
        sa.Column("research_cycle_id", sa.String(160), nullable=False),
        sa.Column("proposal_id", sa.String(160), nullable=False),
        sa.Column("challenger_id", sa.String(160), nullable=False),
        sa.Column("information_role", sa.String(40), nullable=False),
        sa.Column("primary_action_kind", sa.String(50), nullable=False),
        sa.Column("event_kind", sa.String(60), nullable=False),
        sa.Column("experiment_stage", sa.String(40), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "maturity_due_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("maturity_status", sa.String(30), nullable=False),
        sa.Column(
            "eligible_for_meta_training",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column("previous_event_hash", sa.String(64), nullable=True),
        sa.Column(
            "supersedes_event_id",
            sa.String(160),
            sa.ForeignKey(
                f"{EVENT_TABLE}.event_id",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("maturation_input_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "experiment_id",
            "event_sequence",
            name="uq_research_experiment_outcome_sequence",
        ),
        sa.UniqueConstraint(
            "experiment_id",
            "idempotency_key",
            name="uq_research_experiment_outcome_idempotency",
        ),
        sa.CheckConstraint(
            "event_sequence >= 1",
            name="ck_research_experiment_outcome_sequence",
        ),
    )
    op.create_index(
        "ix_research_experiment_outcome_lookup",
        EVENT_TABLE,
        (
            "experiment_id",
            "challenger_id",
            "available_at",
            "maturity_status",
        ),
    )
    op.create_index(
        "ix_research_experiment_outcome_training",
        EVENT_TABLE,
        (
            "information_role",
            "primary_action_kind",
            "eligible_for_meta_training",
            "available_at",
        ),
    )

    op.create_table(
        SNAPSHOT_TABLE,
        sa.Column("snapshot_id", sa.String(160), primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "data_available_cutoff",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("snapshot_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_research_memory_snapshot_cutoff",
        SNAPSHOT_TABLE,
        ("as_of", "data_available_cutoff"),
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
        "ix_research_memory_snapshot_cutoff",
        table_name=SNAPSHOT_TABLE,
    )
    op.drop_table(SNAPSHOT_TABLE)
    op.drop_index(
        "ix_research_experiment_outcome_training",
        table_name=EVENT_TABLE,
    )
    op.drop_index(
        "ix_research_experiment_outcome_lookup",
        table_name=EVENT_TABLE,
    )
    op.drop_table(EVENT_TABLE)
    op.drop_index(
        "ix_research_experiment_action_lookup",
        table_name=ACTION_TABLE,
    )
    op.drop_table(ACTION_TABLE)
    _replace_scheduler_work_kind_constraint(
        dialect=dialect,
        include_recursive_kinds=False,
    )
    _replace_dispatch_target_constraint(
        dialect=dialect,
        include_recursive_targets=False,
    )


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


def _replace_scheduler_work_kind_constraint(
    *,
    dialect: str,
    include_recursive_kinds: bool,
) -> None:
    values = [
        "DAILY_AGGREGATION",
        "WEEKLY_DEEP_RESEARCH",
        "EVIDENCE_TRIGGERED_RESEARCH",
    ]
    if include_recursive_kinds:
        values.extend(
            (
                "OUTCOME_MATURATION",
                "RESEARCH_MEMORY_MATERIALIZATION",
            )
        )
    expression = "work_kind IN (" + ",".join(
        f"'{value}'" for value in values
    ) + ")"
    _replace_append_only_check_constraint(
        dialect=dialect,
        table=SCHEDULER_WORK_TABLE,
        constraint=SCHEDULER_WORK_KIND_CONSTRAINT,
        expression=expression,
    )


def _replace_dispatch_target_constraint(
    *,
    dialect: str,
    include_recursive_targets: bool,
) -> None:
    values = [
        "RESEARCH_DAILY_AGGREGATION_V1",
        "RESEARCH_DEEP_CYCLE_V1",
    ]
    if include_recursive_targets:
        values.extend(
            (
                "RESEARCH_OUTCOME_MATURATION_V1",
                "RESEARCH_MEMORY_MATERIALIZATION_V1",
            )
        )
    expression = "dispatch_target IN (" + ",".join(
        f"'{value}'" for value in values
    ) + ")"
    _replace_append_only_check_constraint(
        dialect=dialect,
        table=DISPATCH_TABLE,
        constraint=DISPATCH_TARGET_CONSTRAINT,
        expression=expression,
    )


def _replace_append_only_check_constraint(
    *,
    dialect: str,
    table: str,
    constraint: str,
    expression: str,
) -> None:
    if dialect == "sqlite":
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table}_no_update"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table}_no_delete"
        )
        with op.batch_alter_table(
            table,
            recreate="always",
        ) as batch:
            batch.drop_constraint(
                constraint,
                type_="check",
            )
            batch.create_check_constraint(
                constraint,
                expression,
            )
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
    elif dialect == "postgresql":
        op.drop_constraint(
            constraint,
            table,
            type_="check",
        )
        op.create_check_constraint(
            constraint,
            table,
            expression,
        )
