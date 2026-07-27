"""Add the append-only Research Plane scheduler and dispatch outbox.

Revision ID: 0012_research_scheduler_v1
Revises: 0011_trusted_promotion_designation
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_research_scheduler_v1"
down_revision: str | None = "0011_trusted_promotion_designation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "research_schedule_work_items",
    "research_work_dispatch_receipts",
    "research_schedule_events",
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(
        "research_schedule_work_items",
        sa.Column("work_item_id", sa.String(100), primary_key=True),
        sa.Column("schema_version", sa.String(50), nullable=False),
        sa.Column("work_kind", sa.String(50), nullable=False),
        sa.Column(
            "idempotency_key",
            sa.String(160),
            nullable=False,
            unique=True,
        ),
        sa.Column("schedule_version", sa.String(80), nullable=False),
        sa.Column(
            "scheduled_for",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "data_available_cutoff",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "calendar_session_id",
            sa.String(100),
            sa.ForeignKey(
                "market_calendar_sessions.calendar_session_id",
                ondelete="RESTRICT",
            ),
        ),
        sa.Column("trigger_manifest_hash", sa.String(64), nullable=False),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False, unique=True),
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
            "work_kind IN "
            "('DAILY_AGGREGATION','WEEKLY_DEEP_RESEARCH',"
            "'EVIDENCE_TRIGGERED_RESEARCH')",
            name="ck_research_schedule_work_kind",
        ),
        sa.CheckConstraint(
            "data_available_cutoff <= scheduled_for",
            name="ck_research_schedule_work_cutoff",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_research_schedule_work_paper_only",
        ),
    )
    op.create_index(
        "ix_research_schedule_work_due",
        "research_schedule_work_items",
        ("scheduled_for", "work_kind", "work_item_id"),
    )
    op.create_table(
        "research_work_dispatch_receipts",
        sa.Column("receipt_id", sa.String(100), primary_key=True),
        sa.Column(
            "work_item_id",
            sa.String(100),
            sa.ForeignKey(
                "research_schedule_work_items.work_item_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(100), nullable=False),
        sa.Column("dispatch_target", sa.String(80), nullable=False),
        sa.Column("work_payload_hash", sa.String(64), nullable=False),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column(
            "idempotency_key",
            sa.String(160),
            nullable=False,
            unique=True,
        ),
        sa.Column("receipt_hash", sa.String(64), nullable=False, unique=True),
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
            "attempt_number >= 1",
            name="ck_research_dispatch_attempt",
        ),
        sa.CheckConstraint(
            "dispatch_target IN "
            "('RESEARCH_DAILY_AGGREGATION_V1',"
            "'RESEARCH_DEEP_CYCLE_V1')",
            name="ck_research_dispatch_target",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_research_dispatch_paper_only",
        ),
        sa.UniqueConstraint(
            "work_item_id",
            "attempt_number",
            name="uq_research_dispatch_work_attempt",
        ),
    )
    op.create_index(
        "ix_research_dispatch_created",
        "research_work_dispatch_receipts",
        ("created_at", "receipt_id"),
    )
    op.create_table(
        "research_schedule_events",
        sa.Column("event_id", sa.String(100), primary_key=True),
        sa.Column(
            "work_item_id",
            sa.String(100),
            sa.ForeignKey(
                "research_schedule_work_items.work_item_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("lease_owner", sa.String(120)),
        sa.Column("lease_token", sa.String(100)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "receipt_id",
            sa.String(100),
            sa.ForeignKey(
                "research_work_dispatch_receipts.receipt_id",
                ondelete="RESTRICT",
            ),
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
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
            "sequence >= 1",
            name="ck_research_schedule_event_sequence",
        ),
        sa.CheckConstraint(
            "attempt_number >= 0",
            name="ck_research_schedule_event_attempt",
        ),
        sa.CheckConstraint(
            "event_type IN "
            "('PLANNED','LEASE_ACQUIRED','LEASE_RECLAIMED',"
            "'DISPATCHED','SUCCEEDED','FAILED')",
            name="ck_research_schedule_event_type",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_research_schedule_event_paper_only",
        ),
        sa.UniqueConstraint(
            "work_item_id",
            "sequence",
            name="uq_research_schedule_event_sequence",
        ),
        sa.UniqueConstraint(
            "work_item_id",
            "idempotency_key",
            name="uq_research_schedule_event_idempotency",
        ),
    )
    op.create_index(
        "ix_research_schedule_event_latest",
        "research_schedule_events",
        ("work_item_id", "sequence", "event_id"),
    )
    op.create_index(
        "ix_research_schedule_event_retry",
        "research_schedule_events",
        ("retryable", "lease_expires_at", "event_type"),
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
        "ix_research_schedule_event_retry",
        table_name="research_schedule_events",
    )
    op.drop_index(
        "ix_research_schedule_event_latest",
        table_name="research_schedule_events",
    )
    op.drop_table("research_schedule_events")
    op.drop_index(
        "ix_research_dispatch_created",
        table_name="research_work_dispatch_receipts",
    )
    op.drop_table("research_work_dispatch_receipts")
    op.drop_index(
        "ix_research_schedule_work_due",
        table_name="research_schedule_work_items",
    )
    op.drop_table("research_schedule_work_items")


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
