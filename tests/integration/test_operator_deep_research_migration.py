from __future__ import annotations

import contextlib
import io
from pathlib import Path

from alembic import command
from sqlalchemy import inspect, text

from trading.persistence.db import (
    alembic_config,
    create_database_engine,
    current_revision,
    downgrade_database,
    upgrade_database,
)

REVISION = "0022_operator_deep_research_work"
DOWN_REVISION = "0021_research_execution_lease_v1"
TABLE = "research_schedule_work_items"
CONSTRAINT = "ck_research_schedule_work_kind"


def test_operator_deep_research_migration_roundtrip_and_guards(
    tmp_path: Path,
) -> None:
    database_url = (
        "sqlite+pysqlite:///"
        f"{(tmp_path / 'operator-deep-research.db').as_posix()}"
    )
    upgrade_database(database_url)
    engine = create_database_engine(database_url)
    assert current_revision(engine) == REVISION
    expression = {
        item["name"]: str(item["sqltext"])
        for item in inspect(engine).get_check_constraints(TABLE)
    }[CONSTRAINT]
    assert "OPERATOR_DEEP_RESEARCH" in expression
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
    assert f"trg_{TABLE}_no_update" in triggers
    assert f"trg_{TABLE}_no_delete" in triggers
    engine.dispose()

    downgrade_database(database_url, DOWN_REVISION)
    downgraded = create_database_engine(database_url)
    assert current_revision(downgraded) == DOWN_REVISION
    downgraded_expression = {
        item["name"]: str(item["sqltext"])
        for item in inspect(downgraded).get_check_constraints(TABLE)
    }[CONSTRAINT]
    assert "OPERATOR_DEEP_RESEARCH" not in downgraded_expression
    downgraded.dispose()

    upgrade_database(database_url)
    upgraded = create_database_engine(database_url)
    assert current_revision(upgraded) == REVISION
    upgraded.dispose()


def test_operator_deep_research_postgresql_offline_ddl() -> None:
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
    assert f"ALTER TABLE {TABLE} DROP CONSTRAINT {CONSTRAINT}" in sql
    assert "OPERATOR_DEEP_RESEARCH" in sql
