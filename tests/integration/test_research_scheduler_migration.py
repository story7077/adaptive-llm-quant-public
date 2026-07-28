from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from trading.persistence.db import (
    create_database_engine,
    downgrade_database,
    upgrade_database,
)


def test_scheduler_migration_downgrade_reupgrade_and_append_only(
    tmp_path: Path,
) -> None:
    database_url = (
        f"sqlite+pysqlite:///{(tmp_path / 'scheduler-migration.db').as_posix()}"
    )
    upgrade_database(database_url)
    engine = create_database_engine(database_url)
    assert {
        "research_schedule_work_items",
        "research_schedule_events",
        "research_work_dispatch_receipts",
    }.issubset(inspect(engine).get_table_names())
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO research_schedule_work_items (
                    work_item_id, schema_version, work_kind,
                    idempotency_key, schedule_version, scheduled_for,
                    data_available_cutoff, calendar_session_id,
                    trigger_manifest_hash, config_manifest_hash,
                    plan_hash, real_order_routing, payload_json, created_at
                ) VALUES (
                    'migration-guard-work', 'research_schedule_plan_v1',
                    'EVIDENCE_TRIGGERED_RESEARCH', 'migration-guard-idem',
                    'research_schedule_v1', '2026-07-27 20:00:00',
                    '2026-07-27 20:00:00', NULL,
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                    'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                    false, '{}', '2026-07-27 20:00:00'
                )
                """
            )
        )
    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text(
                "UPDATE research_schedule_work_items "
                "SET work_kind='DAILY_AGGREGATION'"
            )
        )
    engine.dispose()

    downgrade_database(database_url, "0011_trusted_promotion_designation")
    downgraded = create_database_engine(database_url)
    assert "research_schedule_work_items" not in inspect(
        downgraded
    ).get_table_names()
    downgraded.dispose()

    upgrade_database(database_url)
    upgraded = create_database_engine(database_url)
    assert "research_schedule_events" in inspect(upgraded).get_table_names()
    upgraded.dispose()
