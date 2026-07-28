"""Append-only Alpaca Paper canary routing records.

Revision ID: 0008_alpaca_paper_canary
Revises: 0007_q1_math_core_v1
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_alpaca_paper_canary"
down_revision: str | None = "0007_q1_math_core_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "paper_broker_bindings",
    "paper_broker_commands",
    "paper_broker_events",
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    op.create_table(
        "paper_broker_bindings",
        sa.Column("binding_id", sa.String(100), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("execution_lane", sa.String(40), nullable=False),
        sa.Column("source_arm_id", sa.String(30), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("account_id_hash", sa.String(64), nullable=False),
        sa.Column("base_url", sa.String(120), nullable=False),
        sa.Column(
            "initial_equity_usd",
            sa.Numeric(38, 10),
            nullable=False,
        ),
        sa.Column(
            "initial_cash_usd",
            sa.Numeric(38, 10),
            nullable=False,
        ),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(80), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "execution_lane = 'ALPACA_PAPER_CANARY'",
            name="ck_paper_broker_binding_lane",
        ),
        sa.CheckConstraint(
            "source_arm_id IN ('Q1-DET', 'Q1-LLM')",
            name="ck_paper_broker_binding_source_arm",
        ),
        sa.CheckConstraint(
            "provider = 'ALPACA'",
            name="ck_paper_broker_binding_provider",
        ),
        sa.CheckConstraint(
            "base_url = 'https://paper-api.alpaca.markets'",
            name="ck_paper_broker_binding_base_url",
        ),
        sa.CheckConstraint(
            "initial_equity_usd > 0",
            name="ck_paper_broker_binding_positive_equity",
        ),
        sa.CheckConstraint(
            "initial_cash_usd >= 0",
            name="ck_paper_broker_binding_nonnegative_cash",
        ),
        sa.UniqueConstraint(
            "run_id",
            name="uq_paper_broker_binding_run",
        ),
        sa.UniqueConstraint(
            "binding_hash",
            name="uq_paper_broker_binding_hash",
        ),
    )

    op.create_table(
        "paper_broker_commands",
        sa.Column("command_id", sa.String(100), primary_key=True),
        sa.Column(
            "binding_id",
            sa.String(100),
            sa.ForeignKey(
                "paper_broker_bindings.binding_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_decision_id",
            sa.String(80),
            sa.ForeignKey(
                "portfolio_decisions.portfolio_decision_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("command_type", sa.String(20), nullable=False),
        sa.Column("client_order_id", sa.String(128)),
        sa.Column("broker_order_id", sa.String(100)),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 10), nullable=False),
        sa.Column("limit_price", sa.Numeric(38, 12)),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(80), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "command_type IN ('SUBMIT', 'CANCEL')",
            name="ck_paper_broker_command_type",
        ),
        sa.CheckConstraint(
            "symbol IN ('QQQ', 'SOXX')",
            name="ck_paper_broker_command_symbol",
        ),
        sa.CheckConstraint(
            "side IN ('BUY', 'SELL')",
            name="ck_paper_broker_command_side",
        ),
        sa.CheckConstraint(
            "quantity > 0",
            name="ck_paper_broker_command_positive_quantity",
        ),
        sa.CheckConstraint(
            "limit_price IS NULL OR limit_price > 0",
            name="ck_paper_broker_command_positive_limit",
        ),
        sa.CheckConstraint(
            "(command_type = 'SUBMIT' "
            "AND client_order_id IS NOT NULL "
            "AND broker_order_id IS NULL "
            "AND limit_price IS NOT NULL) "
            "OR (command_type = 'CANCEL' "
            "AND broker_order_id IS NOT NULL)",
            name="ck_paper_broker_command_identity",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_paper_broker_command_idempotency",
        ),
        sa.UniqueConstraint(
            "binding_id",
            "client_order_id",
            "command_type",
            name="uq_paper_broker_command_client_type",
        ),
        sa.UniqueConstraint(
            "binding_id",
            "broker_order_id",
            "command_type",
            name="uq_paper_broker_command_broker_type",
        ),
    )
    op.create_index(
        "ix_paper_broker_command_binding_created",
        "paper_broker_commands",
        ("binding_id", "created_at", "command_id"),
    )
    op.create_index(
        "ix_paper_broker_command_broker_order",
        "paper_broker_commands",
        ("binding_id", "broker_order_id"),
    )
    op.create_index(
        "ix_paper_broker_command_source_decision",
        "paper_broker_commands",
        ("source_decision_id", "created_at"),
    )

    op.create_table(
        "paper_broker_events",
        sa.Column("event_id", sa.String(100), primary_key=True),
        sa.Column(
            "binding_id",
            sa.String(100),
            sa.ForeignKey(
                "paper_broker_bindings.binding_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "command_id",
            sa.String(100),
            sa.ForeignKey(
                "paper_broker_commands.command_id",
                ondelete="RESTRICT",
            ),
        ),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("broker_order_id", sa.String(100)),
        sa.Column("client_order_id", sa.String(128)),
        sa.Column("provider_event_id", sa.String(120)),
        sa.Column("symbol", sa.String(30)),
        sa.Column("side", sa.String(10)),
        sa.Column("quantity", sa.Numeric(38, 10)),
        sa.Column("filled_quantity", sa.Numeric(38, 10)),
        sa.Column("fill_price", sa.Numeric(38, 12)),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("provider_request_id", sa.String(120)),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("config_manifest_hash", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(80), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN "
            "('ACCOUNT_SNAPSHOT', 'POSITIONS_SNAPSHOT', "
            "'ORDER_SNAPSHOT', 'FILL_ACTIVITY', "
            "'SUBMIT_RECONCILED', 'CANCEL_REQUEST_ACCEPTED', "
            "'RECONCILIATION_READY', 'RECONCILIATION_BLOCKED', "
            "'RECONCILIATION_FAILED', 'ORDER_ACKNOWLEDGED', "
            "'ORDER_STATUS', 'CANCEL_ACCEPTED', 'FILL')",
            name="ck_paper_broker_event_type",
        ),
        sa.CheckConstraint(
            "symbol IS NULL OR symbol IN ('QQQ', 'SOXX')",
            name="ck_paper_broker_event_symbol",
        ),
        sa.CheckConstraint(
            "side IS NULL OR side IN ('BUY', 'SELL')",
            name="ck_paper_broker_event_side",
        ),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity > 0",
            name="ck_paper_broker_event_positive_quantity",
        ),
        sa.CheckConstraint(
            "filled_quantity IS NULL OR filled_quantity >= 0",
            name="ck_paper_broker_event_nonnegative_fill",
        ),
        sa.CheckConstraint(
            "fill_price IS NULL OR fill_price > 0",
            name="ck_paper_broker_event_positive_fill_price",
        ),
        sa.CheckConstraint(
            "available_at >= occurred_at",
            name="ck_paper_broker_event_available_after_occurrence",
        ),
        sa.CheckConstraint(
            "created_at >= available_at",
            name="ck_paper_broker_event_created_after_available",
        ),
        sa.CheckConstraint(
            "event_type NOT IN ('FILL', 'FILL_ACTIVITY') OR "
            "(command_id IS NOT NULL "
            "AND provider_event_id IS NOT NULL "
            "AND broker_order_id IS NOT NULL "
            "AND symbol IS NOT NULL "
            "AND side IS NOT NULL "
            "AND quantity IS NOT NULL "
            "AND filled_quantity IS NOT NULL "
            "AND fill_price IS NOT NULL)",
            name="ck_paper_broker_fill_identity",
        ),
        sa.UniqueConstraint(
            "binding_id",
            "idempotency_key",
            name="uq_paper_broker_event_idempotency",
        ),
        sa.UniqueConstraint(
            "binding_id",
            "provider_event_id",
            name="uq_paper_broker_provider_event",
        ),
    )
    op.create_index(
        "ix_paper_broker_event_command_latest",
        "paper_broker_events",
        ("command_id", "available_at", "created_at", "event_id"),
    )
    op.create_index(
        "ix_paper_broker_event_client_latest",
        "paper_broker_events",
        ("binding_id", "client_order_id", "available_at", "event_id"),
    )
    op.create_index(
        "ix_paper_broker_event_broker_latest",
        "paper_broker_events",
        ("binding_id", "broker_order_id", "available_at", "event_id"),
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
        "ix_paper_broker_event_broker_latest",
        table_name="paper_broker_events",
    )
    op.drop_index(
        "ix_paper_broker_event_client_latest",
        table_name="paper_broker_events",
    )
    op.drop_index(
        "ix_paper_broker_event_command_latest",
        table_name="paper_broker_events",
    )
    op.drop_table("paper_broker_events")

    op.drop_index(
        "ix_paper_broker_command_source_decision",
        table_name="paper_broker_commands",
    )
    op.drop_index(
        "ix_paper_broker_command_broker_order",
        table_name="paper_broker_commands",
    )
    op.drop_index(
        "ix_paper_broker_command_binding_created",
        table_name="paper_broker_commands",
    )
    op.drop_table("paper_broker_commands")
    op.drop_table("paper_broker_bindings")


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
