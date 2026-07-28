from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from trading.persistence.db import (
    alembic_config,
    create_database_engine,
    current_revision,
    downgrade_database,
    upgrade_database,
)

REVISION = "0015_meta_controller_v1"
DOWN_REVISION = "0014_experiment_outcome_ledger"
TABLES = ("research_action_plans", "algorithm_proposals_v2")


def test_meta_controller_migration_downgrade_and_reupgrade(
    tmp_path: Path,
) -> None:
    database_url = (
        f"sqlite+pysqlite:///{(tmp_path / 'meta-controller.db').as_posix()}"
    )
    upgrade_database(database_url, REVISION)
    engine = create_database_engine(database_url)
    assert current_revision(engine) == REVISION
    assert set(TABLES).issubset(inspect(engine).get_table_names())
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO research_action_plans (
                    action_plan_id, research_cycle_id, policy_version,
                    research_memory_snapshot_hash, training_view_hash,
                    context_hash, config_hash, plan_hash, idempotency_key,
                    payload_json, generated_at
                ) VALUES (
                    'plan-migration', 'cycle-migration', 'policy-v1',
                    :snapshot_hash, :view_hash, :context_hash, :config_hash,
                    :plan_hash, 'plan-migration-idempotency', '{}',
                    '2026-07-28 00:00:00'
                )
                """
            ),
            {
                "snapshot_hash": "a" * 64,
                "view_hash": "b" * 64,
                "context_hash": "c" * 64,
                "config_hash": "d" * 64,
                "plan_hash": "e" * 64,
            },
        )
    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text(
                "UPDATE research_action_plans "
                "SET policy_version='mutated'"
            )
        )
    engine.dispose()

    downgrade_database(database_url, DOWN_REVISION)
    downgraded = create_database_engine(database_url)
    assert current_revision(downgraded) == DOWN_REVISION
    assert not set(TABLES).intersection(inspect(downgraded).get_table_names())
    downgraded.dispose()

    upgrade_database(database_url, REVISION)
    upgraded = create_database_engine(database_url)
    assert current_revision(upgraded) == REVISION
    assert set(TABLES).issubset(inspect(upgraded).get_table_names())
    upgraded.dispose()


def test_meta_controller_postgresql_offline_ddl_is_append_only() -> None:
    config = alembic_config(
        "postgresql+psycopg://placeholder:placeholder@localhost/placeholder"
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        command.upgrade(
            config,
            f"{DOWN_REVISION}:{REVISION}",
            sql=True,
        )
    sql = output.getvalue()
    for table in TABLES:
        assert f"CREATE TABLE {table}" in sql
        assert f"trg_{table}_append_only" in sql
    assert "uq_research_action_plan_cycle" in sql
    assert "uq_research_action_plan_idempotency" in sql
