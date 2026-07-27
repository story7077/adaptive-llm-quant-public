from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def create_database_engine(database_url: str, *, echo: bool = False) -> Engine:
    kwargs: dict[str, object] = {"echo": echo, "future": True}
    if database_url in {"sqlite://", "sqlite+pysqlite:///:memory:"}:
        kwargs.update(
            {
                "connect_args": {"check_same_thread": False},
                "poolclass": StaticPool,
            }
        )
    engine = create_engine(database_url, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def alembic_config(database_url: str) -> Config:
    repo_root = Path(__file__).resolve().parents[3]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_database(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade_database(database_url: str, revision: str) -> None:
    command.downgrade(alembic_config(database_url), revision)


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        if engine.dialect.name == "sqlite":
            exists = connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='alembic_version'"
                )
            ).scalar_one_or_none()
            if exists is None:
                return None
        else:
            exists = connection.execute(
                text("SELECT to_regclass('public.alembic_version')")
            ).scalar_one_or_none()
            if exists is None:
                return None
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

