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

REVISION = "0014_experiment_outcome_ledger"
TABLES = (
    "research_experiment_actions",
    "research_experiment_outcome_events",
    "research_memory_snapshots",
)


def test_outcome_ledger_migration_downgrade_and_reupgrade(
    tmp_path: Path,
) -> None:
    database_url = (
        f"sqlite+pysqlite:///{(tmp_path / 'outcome-ledger.db').as_posix()}"
    )
    upgrade_database(database_url, REVISION)
    engine = create_database_engine(database_url)
    assert current_revision(engine) == REVISION
    assert set(TABLES).issubset(inspect(engine).get_table_names())
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO research_experiment_actions (
                    action_id, experiment_id, research_cycle_id, proposal_id,
                    challenger_id, information_role, primary_action_kind,
                    maturity_due_at, meta_training_permitted,
                    idempotency_key, action_hash, payload_json, created_at
                ) VALUES (
                    'migration-action', 'migration-experiment',
                    'migration-cycle', 'migration-proposal',
                    'migration-challenger', 'DISCOVERY', 'ADD_FEATURE',
                    '2026-07-28 00:00:00', false, 'migration-idempotency',
                    :action_hash, '{}', '2026-07-28 00:00:00'
                )
                """
            ),
            {"action_hash": "a" * 64},
        )
    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text(
                "UPDATE research_experiment_actions "
                "SET primary_action_kind='REMOVE_FEATURE'"
            )
        )
    engine.dispose()

    downgrade_database(database_url, "0013_candidate_artifact_registry")
    downgraded = create_database_engine(database_url)
    assert not set(TABLES).intersection(inspect(downgraded).get_table_names())
    downgraded.dispose()

    upgrade_database(database_url, REVISION)
    upgraded = create_database_engine(database_url)
    assert current_revision(upgraded) == REVISION
    assert set(TABLES).issubset(inspect(upgraded).get_table_names())
    upgraded.dispose()


def test_outcome_ledger_postgresql_offline_ddl_is_append_only() -> None:
    config = alembic_config(
        "postgresql+psycopg://placeholder:placeholder@localhost/placeholder"
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        command.upgrade(
            config,
            "0013_candidate_artifact_registry:0014_experiment_outcome_ledger",
            sql=True,
        )
    sql = output.getvalue()
    for table in TABLES:
        assert f"CREATE TABLE {table}" in sql
        assert f"trg_{table}_append_only" in sql
    assert "uq_research_experiment_outcome_sequence" in sql
    assert "uq_research_experiment_outcome_idempotency" in sql
    assert "OUTCOME_MATURATION" in sql
    assert "RESEARCH_MEMORY_MATERIALIZATION" in sql
    assert "RESEARCH_OUTCOME_MATURATION_V1" in sql
    assert "RESEARCH_MEMORY_MATERIALIZATION_V1" in sql
