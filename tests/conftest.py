from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from trading.persistence.db import (
    create_database_engine,
    make_session_factory,
    upgrade_database,
)
from trading.runtime.pipeline import seed_demo
from trading.settings import ConfigBundle, Settings, load_config_bundle


@pytest.fixture(scope="session")
def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def config_bundle(repository_root: Path) -> ConfigBundle:
    return load_config_bundle(repository_root / "config")


@pytest.fixture()
def sqlite_database(
    tmp_path: Path,
) -> Iterator[tuple[str, Engine, sessionmaker[Session]]]:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'phase0.db').as_posix()}"
    upgrade_database(database_url)
    engine = create_database_engine(database_url)
    factory = make_session_factory(engine)
    yield database_url, engine, factory
    engine.dispose()


@pytest.fixture()
def seeded_demo(
    repository_root: Path,
    config_bundle: ConfigBundle,
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    tmp_path: Path,
) -> tuple[Settings, Engine, sessionmaker[Session], dict[str, object], str]:
    database_url, engine, factory = sqlite_database
    settings = Settings(
        database_url=database_url,
        config_dir=repository_root / "config",
        raw_store=tmp_path / "raw",
        real_broker_enabled=False,
        real_llm_enabled=False,
        production_unlock=False,
    )
    manifest, result_hash, created = seed_demo(
        settings=settings,
        config=config_bundle,
        session_factory=factory,
    )
    if not created:
        raise AssertionError("Fresh test database unexpectedly contained demo_run")
    return settings, engine, factory, manifest, result_hash

