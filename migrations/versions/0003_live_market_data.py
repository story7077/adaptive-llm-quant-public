"""Append-only Alpaca IEX market data.

Revision ID: 0003_live_market_data
Revises: 0002_adaptive_control_plane
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_live_market_data"
down_revision: str | None = "0002_adaptive_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "market_bars",
    "market_quotes",
    "market_trade_events",
)


def upgrade() -> None:
    op.create_table(
        "market_bars",
        sa.Column("bar_id", sa.String(80), primary_key=True),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("feed", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("timeframe", sa.String(20), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_timestamp", sa.String(50), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_kind", sa.String(30), nullable=False),
        sa.Column("open", sa.Numeric(38, 12), nullable=False),
        sa.Column("high", sa.Numeric(38, 12), nullable=False),
        sa.Column("low", sa.Numeric(38, 12), nullable=False),
        sa.Column("close", sa.Numeric(38, 12), nullable=False),
        sa.Column("volume", sa.Numeric(38, 10), nullable=False),
        sa.Column("vwap", sa.Numeric(38, 12)),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(100)),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("raw_object_uri", sa.String(500)),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "provider",
            "feed",
            "symbol",
            "timeframe",
            "provider_timestamp",
            "payload_hash",
            name="uq_market_bar_payload",
        ),
    )
    op.create_index(
        "ix_market_bars_pit",
        "market_bars",
        ("provider", "feed", "symbol", "timeframe", "event_time", "available_at"),
    )
    op.create_table(
        "market_quotes",
        sa.Column("quote_id", sa.String(80), primary_key=True),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("feed", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_timestamp", sa.String(50), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_kind", sa.String(30), nullable=False),
        sa.Column("bid_exchange", sa.String(20)),
        sa.Column("bid_price", sa.Numeric(38, 12), nullable=False),
        sa.Column("bid_size_round_lots", sa.Integer(), nullable=False),
        sa.Column("ask_exchange", sa.String(20)),
        sa.Column("ask_price", sa.Numeric(38, 12), nullable=False),
        sa.Column("ask_size_round_lots", sa.Integer(), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("tape", sa.String(10)),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("raw_object_uri", sa.String(500)),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "provider",
            "feed",
            "symbol",
            "provider_timestamp",
            "payload_hash",
            name="uq_market_quote_payload",
        ),
    )
    op.create_index(
        "ix_market_quotes_pit",
        "market_quotes",
        ("provider", "feed", "symbol", "event_time", "available_at"),
    )
    op.create_table(
        "market_trade_events",
        sa.Column("trade_event_id", sa.String(80), primary_key=True),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("feed", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("event_kind", sa.String(30), nullable=False),
        sa.Column("provider_event_id", sa.String(100)),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_timestamp", sa.String(50), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_kind", sa.String(30), nullable=False),
        sa.Column("exchange", sa.String(20)),
        sa.Column("price", sa.Numeric(38, 12)),
        sa.Column("size", sa.Numeric(38, 10)),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("tape", sa.String(10)),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("raw_object_uri", sa.String(500)),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "provider",
            "feed",
            "symbol",
            "event_kind",
            "provider_timestamp",
            "payload_hash",
            name="uq_market_trade_event_payload",
        ),
    )
    op.create_index(
        "ix_market_trade_events_pit",
        "market_trade_events",
        ("provider", "feed", "symbol", "event_time", "available_at"),
    )
    op.create_table(
        "market_stream_status",
        sa.Column("provider", sa.String(40), primary_key=True),
        sa.Column("feed", sa.String(20), primary_key=True),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True)),
        sa.Column("disconnected_at", sa.DateTime(timezone=True)),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("last_bar_at", sa.DateTime(timezone=True)),
        sa.Column("last_quote_at", sa.DateTime(timezone=True)),
        sa.Column("last_trade_at", sa.DateTime(timezone=True)),
        sa.Column("reconnect_count", sa.Integer(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("last_error_detail", sa.String(500)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    _create_append_only_guards()


def downgrade() -> None:
    _drop_append_only_guards()
    op.drop_table("market_stream_status")
    op.drop_index("ix_market_trade_events_pit", table_name="market_trade_events")
    op.drop_table("market_trade_events")
    op.drop_index("ix_market_quotes_pit", table_name="market_quotes")
    op.drop_table("market_quotes")
    op.drop_index("ix_market_bars_pit", table_name="market_bars")
    op.drop_table("market_bars")


def _create_append_only_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for table in APPEND_ONLY_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()
                """
            )
    elif dialect == "sqlite":
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


def _drop_append_only_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for table in APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    elif dialect == "sqlite":
        for table in APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
