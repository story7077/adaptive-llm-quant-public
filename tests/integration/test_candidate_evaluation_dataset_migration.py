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

REVISION = "0020_candidate_evaluation_dataset_v2"
HEAD_REVISION = "0021_research_execution_lease_v1"
DOWN_REVISION = "0019_candidate_prospective_outcomes_v1"
TABLES = (
    "research_candidate_evaluation_datasets_v2",
    "research_candidate_evaluation_traces_v2",
)


def test_candidate_evaluation_dataset_migration_roundtrip_and_guards(
    tmp_path: Path,
) -> None:
    database_url = (
        "sqlite+pysqlite:///"
        f"{(tmp_path / 'candidate-evaluation-v2.db').as_posix()}"
    )
    upgrade_database(database_url)
    engine = create_database_engine(database_url)
    assert current_revision(engine) == HEAD_REVISION
    assert set(TABLES).issubset(inspect(engine).get_table_names())

    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger'"
                )
            )
        }
        for table in TABLES:
            assert f"trg_{table}_no_update" in triggers
            assert f"trg_{table}_no_delete" in triggers

        # The trigger contract is independent of parent-table foreign keys.
        # Disable FK checks only for these synthetic trigger-probe rows.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        assert connection.exec_driver_sql(
            "PRAGMA foreign_keys"
        ).scalar_one() == 0
        connection.execute(
            text(
                "INSERT INTO research_candidate_evaluation_datasets_v2 "
                "(dataset_id, challenger_id, candidate_artifact_hash, "
                "source_manifest_hash, config_manifest_hash, "
                "base_session_count, scenario_count, dataset_hash, "
                "real_order_routing, payload_json, created_at) VALUES "
                "('dataset-trigger-probe', 'challenger-trigger-probe', "
                ":artifact, :source, :config, 126, 126, :dataset, "
                "false, '{}', '2026-07-29 00:00:00')"
            ),
            {
                "artifact": "a" * 64,
                "source": "b" * 64,
                "config": "c" * 64,
                "dataset": "d" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO research_candidate_evaluation_traces_v2 "
                "(trace_id, dataset_id, challenger_id, "
                "candidate_artifact_hash, source_manifest_hash, "
                "evaluation_contract_hash, replay_artifact_hash, "
                "trace_hash, real_order_routing, payload_json, created_at) "
                "VALUES ('trace-trigger-probe', 'dataset-trigger-probe', "
                "'challenger-trigger-probe', :artifact, :source, "
                ":contract, :replay, :trace, false, '{}', "
                "'2026-07-29 00:00:00')"
            ),
            {
                "artifact": "a" * 64,
                "source": "b" * 64,
                "contract": "e" * 64,
                "replay": "f" * 64,
                "trace": "1" * 64,
            },
        )
        connection.commit()

        for table in TABLES:
            with pytest.raises(DBAPIError, match="append-only"):
                connection.execute(
                    text(
                        f"UPDATE {table} "
                        "SET payload_json=payload_json"
                    )
                )
                connection.commit()
            connection.rollback()
            with pytest.raises(DBAPIError, match="append-only"):
                connection.execute(text(f"DELETE FROM {table}"))
                connection.commit()
            connection.rollback()
    engine.dispose()

    downgrade_database(database_url, DOWN_REVISION)
    downgraded = create_database_engine(database_url)
    assert current_revision(downgraded) == DOWN_REVISION
    assert not set(TABLES).intersection(
        inspect(downgraded).get_table_names()
    )
    downgraded.dispose()

    upgrade_database(database_url)
    upgraded = create_database_engine(database_url)
    assert current_revision(upgraded) == HEAD_REVISION
    assert set(TABLES).issubset(inspect(upgraded).get_table_names())
    upgraded.dispose()


def test_candidate_evaluation_dataset_postgresql_ddl_is_append_only() -> None:
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
        assert "recorded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in sql
    assert "ck_candidate_evaluation_dataset_v2_sessions" in sql
    assert "ck_candidate_evaluation_dataset_v2_paper_only" in sql
    assert "ck_candidate_evaluation_trace_v2_paper_only" in sql
