from __future__ import annotations

import contextlib
import inspect
import io
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from trading.cli import _is_local_database
from trading.control.providers import CommanderProvider
from trading.control.service import ControlPlaneService
from trading.persistence import q1 as q1_persistence
from trading.persistence.db import (
    alembic_config,
    create_database_engine,
    current_revision,
    downgrade_database,
    upgrade_database,
)
from trading.persistence.models import SourceRecordRow
from trading.persistence.repositories import SourceRecordRepository


def test_cli_downgrade_gate_accepts_only_sqlite() -> None:
    assert _is_local_database("sqlite+pysqlite:///disposable.db")
    assert not _is_local_database(
        "postgresql+psycopg://postgres@127.0.0.1:55432/trading_phase0"
    )
    assert not _is_local_database(
        "postgresql+psycopg://postgres@localhost/disposable"
    )


def test_migration_upgrade_downgrade_round_trip(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{(tmp_path / 'migration.db').as_posix()}"
    upgrade_database(url)
    engine = create_database_engine(url)
    assert current_revision(engine) == "0022_operator_deep_research_work"
    engine.dispose()

    downgrade_database(url, "base")
    engine = create_database_engine(url)
    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert "runs" not in tables
    engine.dispose()

    upgrade_database(url)
    engine = create_database_engine(url)
    assert current_revision(engine) == "0022_operator_deep_research_work"
    engine.dispose()


def test_q1_migration_preserves_legacy_append_only_rows(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{(tmp_path / 'legacy-to-q1.db').as_posix()}"
    upgrade_database(url, "0006_forward_paper_execution")
    engine = create_database_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runs "
                "(run_id, mode, experiment_version, config_manifest_hash, code_commit, "
                "started_at, status) VALUES "
                "('legacy-run', 'PAPER', 'legacy-forward', :hash, 'legacy-code', "
                "'2026-07-27 00:00:00', 'STOPPED')"
            ),
            {"hash": "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO paper_cycles "
                "(cycle_id, run_id, cycle_kind, scheduled_at, status, "
                "idempotency_key, attempt_count, created_at, updated_at) VALUES "
                "('legacy-cycle', 'legacy-run', 'DECISION', "
                "'2026-07-27 14:00:00', 'COMPLETED', 'legacy-cycle', 1, "
                "'2026-07-27 14:00:00', '2026-07-27 14:01:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO portfolio_decisions "
                "(portfolio_decision_id, run_id, arm_id, source_cycle_id, "
                "input_state_sequence, decision_time, payload_json, decision_hash) "
                "VALUES ('legacy-decision', 'legacy-run', 'B0-CASH', "
                "'legacy-cycle', 1, '2026-07-27 14:00:00', '{}', :hash)"
            ),
            {"hash": "c" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO order_intents "
                "(order_intent_id, run_id, arm_id, source_cycle_id, "
                "input_state_sequence, idempotency_key, payload_json, intent_hash) "
                "VALUES ('legacy-order', 'legacy-run', 'B0-CASH', "
                "'legacy-cycle', 1, 'legacy-order', '{}', :hash)"
            ),
            {"hash": "b" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO fills "
                "(fill_id, order_intent_id, run_id, arm_id, source_cycle_id, "
                "effective_at, payload_json) VALUES "
                "('legacy-fill', 'legacy-order', 'legacy-run', 'B0-CASH', "
                "'legacy-cycle', '2026-07-27 14:01:00', '{}')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO nav_snapshots "
                "(nav_snapshot_id, run_id, arm_id, source_cycle_id, as_of, "
                "nav_usd, payload_json) VALUES "
                "('legacy-nav', 'legacy-run', 'B0-CASH', 'legacy-cycle', "
                "'2026-07-27 14:01:00', 10000, '{}')"
            )
        )
    engine.dispose()

    upgrade_database(url)
    engine = create_database_engine(url)
    assert current_revision(engine) == "0022_operator_deep_research_work"
    with engine.connect() as connection:
        decision = connection.execute(
            text(
                "SELECT portfolio_decision_id, algorithm_version, "
                "input_manifest_hash FROM portfolio_decisions "
                "WHERE portfolio_decision_id='legacy-decision'"
            )
        ).one()
        order = connection.execute(
            text(
                "SELECT order_intent_id, algorithm_version, config_manifest_hash "
                "FROM order_intents WHERE order_intent_id='legacy-order'"
            )
        ).one()
        fill = connection.execute(
            text(
                "SELECT fill_id, algorithm_version, base_fill_cost_usd "
                "FROM fills WHERE fill_id='legacy-fill'"
            )
        ).one()
        nav = connection.execute(
            text(
                "SELECT nav_snapshot_id, algorithm_version, config_manifest_hash "
                "FROM nav_snapshots WHERE nav_snapshot_id='legacy-nav'"
            )
        ).one()
    assert tuple(decision) == ("legacy-decision", None, None)
    assert tuple(order) == ("legacy-order", None, None)
    assert tuple(fill) == ("legacy-fill", None, None)
    assert tuple(nav) == ("legacy-nav", None, None)
    engine.dispose()

    downgrade_database(url, "0006_forward_paper_execution")
    engine = create_database_engine(url)
    assert current_revision(engine) == "0006_forward_paper_execution"
    with engine.connect() as connection:
        preserved = {
            table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in (
                "portfolio_decisions",
                "order_intents",
                "fills",
                "nav_snapshots",
            )
        }
    assert preserved == {
        "portfolio_decisions": 1,
        "order_intents": 1,
        "fills": 1,
        "nav_snapshots": 1,
    }
    for table in preserved:
        with (
            engine.connect() as connection,
            connection.begin(),
            pytest.raises(DBAPIError, match="append-only"),
        ):
            connection.execute(text(f"UPDATE {table} SET payload_json=payload_json"))
    engine.dispose()

    upgrade_database(url)
    engine = create_database_engine(url)
    assert current_revision(engine) == "0022_operator_deep_research_work"
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM order_intents "
                    "WHERE order_intent_id='legacy-order' AND algorithm_version IS NULL"
                )
            ).scalar_one()
            == 1
        )
    engine.dispose()


def test_q1_postgresql_offline_ddl_and_database_clock_fence() -> None:
    config = alembic_config("postgresql+psycopg://placeholder:placeholder@localhost/placeholder")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        command.upgrade(
            config,
            "0006_forward_paper_execution:0007_q1_math_core_v1",
            sql=True,
        )
    sql = output.getvalue()
    for table in (
        "market_calendar_sessions",
        "strategy_evaluation_anchors",
        "risk_episodes",
        "risk_episode_targets",
        "risk_episode_events",
        "order_events",
        "cash_settlement_events",
        "strategy_daily_results",
        "matched_attribution_results",
    ):
        assert f"CREATE TABLE {table}" in sql
        assert f"trg_{table}_append_only" in sql
    assert "ALTER TABLE portfolio_decisions ADD COLUMN signal_data_cutoff" in sql
    assert "ALTER TABLE fills ADD COLUMN sensitivity_10bp_cost_usd" in sql

    fence_source = inspect.getsource(q1_persistence._require_cycle_fence)
    assert "with_for_update()" in fence_source
    assert "func.clock_timestamp()" in fence_source


def test_alpaca_paper_postgresql_offline_ddl_is_append_only() -> None:
    config = alembic_config("postgresql+psycopg://placeholder:placeholder@localhost/placeholder")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        command.upgrade(
            config,
            "0007_q1_math_core_v1:0008_alpaca_paper_canary",
            sql=True,
        )
    sql = output.getvalue()
    for table in (
        "paper_broker_bindings",
        "paper_broker_commands",
        "paper_broker_events",
    ):
        assert f"CREATE TABLE {table}" in sql
        assert f"trg_{table}_append_only" in sql


def test_research_plane_postgresql_offline_ddl_is_append_only() -> None:
    config = alembic_config("postgresql+psycopg://placeholder:placeholder@localhost/placeholder")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        command.upgrade(
            config,
            "0008_alpaca_paper_canary:0009_research_plane_v1",
            sql=True,
        )
    sql = output.getvalue()
    for table in (
        "research_commander_selections",
        "research_cycles",
        "research_cycle_events",
        "research_evidence_sources",
        "algorithm_proposals",
        "challenger_manifests",
        "challenger_events",
        "experiment_budget_events",
        "oos_lockbox_results",
        "research_promotion_decisions",
    ):
        assert f"CREATE TABLE {table}" in sql
        assert f"trg_{table}_append_only" in sql


def test_oos_production_lockbox_postgresql_offline_ddl_is_append_only() -> None:
    config = alembic_config("postgresql+psycopg://placeholder:placeholder@localhost/placeholder")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        command.upgrade(
            config,
            "0009_research_plane_v1:0010_oos_production_lockbox",
            sql=True,
        )
    sql = output.getvalue()
    assert "CREATE TABLE oos_budget_reservations" in sql
    assert "trg_oos_budget_reservations_append_only" in sql
    assert "uq_oos_budget_reservation_budget_ordinal" in sql


def test_trusted_promotion_postgresql_offline_ddl_is_append_only() -> None:
    config = alembic_config("postgresql+psycopg://placeholder:placeholder@localhost/placeholder")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        command.upgrade(
            config,
            ("0010_oos_production_lockbox:0011_trusted_promotion_designation"),
            sql=True,
        )
    sql = output.getvalue()
    for table in (
        "research_shadow_performance_summaries",
        "research_promotion_evidence",
        "trusted_promotion_evaluations",
        "research_champion_designations",
    ):
        assert f"CREATE TABLE {table}" in sql
        assert f"trg_{table}_append_only" in sql
    assert "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)" in sql
    assert "automatic_promotion_enabled = false" in sql
    assert "real_order_routing = false" in sql


def test_candidate_artifact_postgresql_offline_ddl_is_append_only() -> None:
    config = alembic_config("postgresql+psycopg://placeholder:placeholder@localhost/placeholder")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        command.upgrade(
            config,
            ("0012_research_scheduler_v1:0013_candidate_artifact_registry"),
            sql=True,
        )
    sql = output.getvalue()
    assert "CREATE TABLE research_candidate_artifacts" in sql
    assert "trg_research_candidate_artifacts_append_only" in sql
    assert "real_order_routing = false" in sql


def test_append_only_db_trigger_rejects_update(seeded_demo) -> None:
    _, engine, _, _, _ = seeded_demo
    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text("UPDATE source_records SET provider='mutated' WHERE external_id='demo_run'")
        )


def test_append_only_repository_has_no_mutation_api() -> None:
    assert not hasattr(SourceRecordRepository, "update")
    assert not hasattr(SourceRecordRepository, "delete")
    assert not hasattr(SourceRecordRow, "mutate")


def test_commander_selection_is_append_only(sqlite_database) -> None:
    _, engine, factory = sqlite_database
    ControlPlaneService(factory).select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
    )
    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text("UPDATE commander_selections SET provider='WEBGPT_SOL_PRO' WHERE version=1")
        )
