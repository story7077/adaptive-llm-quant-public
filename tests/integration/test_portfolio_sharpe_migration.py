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

REVISION = "0016_portfolio_delta_sharpe_v2"
DOWN_REVISION = "0015_meta_controller_v1"
TABLE = "portfolio_comparison_contracts"


def test_portfolio_sharpe_migration_downgrade_and_reupgrade(
    tmp_path: Path,
) -> None:
    database_url = (
        f"sqlite+pysqlite:///{(tmp_path / 'portfolio-sharpe.db').as_posix()}"
    )
    upgrade_database(database_url, REVISION)
    engine = create_database_engine(database_url)
    assert current_revision(engine) == REVISION
    assert TABLE in inspect(engine).get_table_names()
    engine.dispose()

    downgrade_database(database_url, DOWN_REVISION)
    downgraded = create_database_engine(database_url)
    assert current_revision(downgraded) == DOWN_REVISION
    assert TABLE not in inspect(downgraded).get_table_names()
    downgraded.dispose()

    upgrade_database(database_url, REVISION)
    upgraded = create_database_engine(database_url)
    assert current_revision(upgraded) == REVISION
    assert TABLE in inspect(upgraded).get_table_names()
    upgraded.dispose()


def test_portfolio_sharpe_postgresql_offline_ddl_is_append_only() -> None:
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
    assert f"CREATE TABLE {TABLE}" in sql
    assert f"trg_{TABLE}_append_only" in sql
    assert "uq_portfolio_comparison_challenger_artifact" in sql
    assert "ck_portfolio_comparison_pit" in sql
