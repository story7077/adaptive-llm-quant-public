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

REVISION = "0017_chronological_meta_oos_v1"
DOWN_REVISION = "0016_portfolio_delta_sharpe_v2"
TABLES = (
    "chronological_meta_oos_plans",
    "meta_oos_outer_audit_reservations",
    "meta_oos_epoch_arm_audit_records",
    "chronological_meta_oos_results",
)


def test_meta_oos_migration_downgrade_and_reupgrade(
    tmp_path: Path,
) -> None:
    database_url = (
        f"sqlite+pysqlite:///{(tmp_path / 'meta-oos.db').as_posix()}"
    )
    upgrade_database(database_url)
    engine = create_database_engine(database_url)
    assert current_revision(engine) == REVISION
    assert set(TABLES).issubset(inspect(engine).get_table_names())
    engine.dispose()

    downgrade_database(database_url, DOWN_REVISION)
    downgraded = create_database_engine(database_url)
    assert current_revision(downgraded) == DOWN_REVISION
    assert not set(TABLES).intersection(inspect(downgraded).get_table_names())
    downgraded.dispose()

    upgrade_database(database_url)
    upgraded = create_database_engine(database_url)
    assert current_revision(upgraded) == REVISION
    assert set(TABLES).issubset(inspect(upgraded).get_table_names())
    upgraded.dispose()


def test_meta_oos_postgresql_offline_ddl_is_append_only() -> None:
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
    assert "uq_meta_oos_plan_dataset_budget" in sql
    assert "uq_meta_oos_reservation_dataset_budget" in sql
    assert "uq_meta_oos_epoch_arm_record" in sql
