from __future__ import annotations

import contextlib
import io
from pathlib import Path

from alembic import command
from sqlalchemy import inspect

from trading.persistence.db import (
    alembic_config,
    create_database_engine,
    current_revision,
    downgrade_database,
    upgrade_database,
)

REVISION = "0019_candidate_prospective_outcomes_v1"
HEAD_REVISION = "0022_operator_deep_research_work"
DOWN_REVISION = "0018_candidate_prospective_v1"
TABLES = (
    "research_candidate_prospective_outcomes",
    "research_candidate_prospective_outcome_failures",
)


def test_candidate_prospective_outcome_migration_roundtrip(
    tmp_path: Path,
) -> None:
    database_url = (
        f"sqlite+pysqlite:///{(tmp_path / 'prospective-outcome.db').as_posix()}"
    )
    upgrade_database(database_url)
    engine = create_database_engine(database_url)
    assert current_revision(engine) == HEAD_REVISION
    assert set(TABLES).issubset(inspect(engine).get_table_names())
    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns(TABLES[0])
    }
    assert columns["recorded_at"]["nullable"] is False
    assert columns["recorded_at"]["default"] is not None
    assert columns["outcome_data_cutoff"]["nullable"] is False
    assert columns["calendar_version"]["nullable"] is False
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


def test_candidate_prospective_outcome_postgresql_ddl_is_append_only() -> None:
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
    assert "uq_candidate_prospective_outcome_request" in sql
    assert "ck_candidate_prospective_outcome_paper_only" in sql
    assert "uq_candidate_prospective_outcome_failure_request" in sql
    assert "ck_candidate_prospective_outcome_failure_paper_only" in sql
    assert "recorded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in sql
