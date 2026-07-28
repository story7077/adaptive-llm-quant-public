from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from trading.cli import app
from trading.domain.hashing import canonical_hash
from trading.persistence.meta_oos import (
    MetaOosPersistenceError,
    MetaOosRepository,
)
from trading.persistence.models import ResearchExperimentOutcomeEventRow
from trading.research.chronological_meta_oos import (
    META_OOS_POLICY_ARMS,
    DeterministicSyntheticMetaOosEnvironment,
    FixedRecalibrationPolicyAdapter,
    MetaOosAuditMode,
    MetaOosEpochV1,
    MetaOosError,
    MetaOosPolicyArm,
    StaticChampionPolicyAdapter,
    SyntheticPolicyAdapter,
    build_chronological_meta_oos_plan,
    build_meta_oos_commander_binding,
    build_meta_oos_evaluation_contract,
    run_chronological_meta_oos,
)
from trading.research.portfolio_delta_sharpe import (
    StationaryBootstrapContractV1,
)

BASE = datetime(2020, 1, 1, tzinfo=UTC)


def _contract(
    version: str = "chronological-meta-oos-thresholds-v1",
):
    return build_meta_oos_evaluation_contract(
        contract_version=version,
        annualization_sessions=252,
        minimum_epochs=8,
        maximum_epochs=52,
        maximum_candidate_generation_budget_per_epoch=10,
        maximum_oos_budget_per_epoch=3,
        maximum_outer_audit_uses_per_dataset=1,
        reservation_ttl_hours=24,
        minimum_adaptive_delta_sharpe_lcb=0.0,
        minimum_research_efficiency=0.0,
        maximum_allowed_drawdown=0.25,
        tail_quantile=0.05,
        maximum_absolute_daily_return=1.0,
    )


def _epochs() -> tuple[MetaOosEpochV1, ...]:
    values: list[MetaOosEpochV1] = []
    for index in range(8):
        start = BASE + timedelta(days=index * 60)
        values.append(
            MetaOosEpochV1(
                epoch_id=f"epoch-{index + 1:02d}",
                discovery_window_start=start,
                discovery_window_end=start + timedelta(days=10),
                decision_at=start + timedelta(days=15),
                maximum_candidate_horizon_sessions=5,
                purge_sessions=5,
                embargo_sessions=2,
                forward_window_start=start + timedelta(days=20),
                forward_window_end=start + timedelta(days=31),
                outcome_available_at=start + timedelta(days=32),
                market_data_manifest_hash=canonical_hash({"epoch": index}),
                context_key="REGIME_X" if index % 2 == 0 else "REGIME_Y",
                candidate_generation_budget=2,
                oos_budget=1,
            )
        )
    return tuple(values)


def _plan(
    *,
    plan_id: str = "meta-oos-plan-1",
    dataset_id: str = "outer-audit-dataset-1",
    budget_ordinal: int = 1,
    contract_version: str = "chronological-meta-oos-thresholds-v1",
    bootstrap_seed: int = 7077,
):
    contract = _contract(contract_version)
    commander = build_meta_oos_commander_binding(
        model_family="GPT-5.6-SOL",
        model_version="gpt-5.6-sol",
        reasoning_profile="max",
        prompt_template_hash="a" * 64,
        request_schema_hash="b" * 64,
        output_schema_hash="c" * 64,
    )
    plan = build_chronological_meta_oos_plan(
        plan_id=plan_id,
        plan_version="chronological-meta-oos-v1",
        initial_champion_manifest_hash="d" * 64,
        epochs=_epochs(),
        policy_adapter_versions={
            MetaOosPolicyArm.STATIC_CHAMPION: "static-champion-v1",
            MetaOosPolicyArm.FIXED_RECALIBRATION: "fixed-recalibration-v1",
            MetaOosPolicyArm.MEMORYLESS_COMMANDER: (
                "synthetic-context-learning-v1"
            ),
            MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER: (
                "synthetic-context-learning-v1"
            ),
        },
        audit_mode=MetaOosAuditMode.SYNTHETIC_FIXTURE,
        commander_binding=commander,
        meta_controller_version="hierarchical-contextual-ucb-v1",
        cost_model_hash="e" * 64,
        execution_model_hash="f" * 64,
        bootstrap_contract=StationaryBootstrapContractV1(
            configured_seed=bootstrap_seed,
            samples=200,
            expected_block_sessions=5,
            lower_quantile=0.025,
            variance_epsilon=1e-12,
        ),
        evaluation_contract_hash=contract.contract_hash,
        outer_audit_dataset_id=dataset_id,
        outer_audit_budget_ordinal=budget_ordinal,
        created_at=BASE - timedelta(days=1),
    )
    return plan, contract


def _run(plan, contract, reservation_hash: str):
    epochs = _epochs()
    adapters = {
        MetaOosPolicyArm.STATIC_CHAMPION: StaticChampionPolicyAdapter(),
        MetaOosPolicyArm.FIXED_RECALIBRATION: (
            FixedRecalibrationPolicyAdapter(
                {epoch.epoch_id: "ACTION_A" for epoch in epochs}
            )
        ),
        MetaOosPolicyArm.MEMORYLESS_COMMANDER: (
            SyntheticPolicyAdapter(("ACTION_A", "ACTION_B"))
        ),
        MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER: (
            SyntheticPolicyAdapter(("ACTION_A", "ACTION_B"))
        ),
    }
    return run_chronological_meta_oos(
        plan=plan,
        evaluation_contract=contract,
        adapters=adapters,
        environment=DeterministicSyntheticMetaOosEnvironment(
            context_action_edge={
                "REGIME_X": "ACTION_A",
                "REGIME_Y": "ACTION_B",
            }
        ),
        outer_audit_reservation_hash=reservation_hash,
        evaluated_at=plan.epochs[-1].outcome_available_at,
    )


def test_meta_oos_plan_reservation_and_result_are_isolated_append_only(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
) -> None:
    _, engine, factory = sqlite_database
    repository = MetaOosRepository(factory)
    plan, contract = _plan()
    assert repository.store_plan(plan, contract)
    assert not repository.store_plan(plan, contract)
    reservation_created_at = plan.epochs[-1].outcome_available_at
    reservation_expires_at = reservation_created_at + timedelta(hours=24)
    with pytest.raises(
        MetaOosPersistenceError,
        match="reservation TTL exceeds contract",
    ):
        repository.reserve_outer_audit(
            plan_id=plan.plan_id,
            idempotency_key="outer-audit-overlong",
            maximum_dataset_uses=1,
            maximum_ttl_hours=24,
            created_at=reservation_created_at,
            expires_at=reservation_created_at + timedelta(hours=25),
        )
    reservation, created = repository.reserve_outer_audit(
        plan_id=plan.plan_id,
        idempotency_key="outer-audit-run-1",
        maximum_dataset_uses=1,
        maximum_ttl_hours=24,
        created_at=reservation_created_at,
        expires_at=reservation_expires_at,
    )
    assert created
    assert repository.reserve_outer_audit(
        plan_id=plan.plan_id,
        idempotency_key="outer-audit-run-1",
        maximum_dataset_uses=1,
        maximum_ttl_hours=24,
        created_at=reservation_created_at,
        expires_at=reservation.expires_at,
    ) == (reservation, False)

    with factory() as session:
        production_events_before = int(
            session.scalar(
                select(func.count()).select_from(
                    ResearchExperimentOutcomeEventRow
                )
            )
            or 0
        )
    run = _run(plan, contract, reservation.reservation_hash)
    assert len(run.audit_records) == len(plan.epochs) * len(
        META_OOS_POLICY_ARMS
    )
    assert repository.store_run(
        run=run,
        reservation=reservation,
        evaluation_contract=contract,
        created_at=run.result.evaluated_at,
    )
    assert not repository.store_run(
        run=run,
        reservation=reservation,
        evaluation_contract=contract,
        created_at=run.result.evaluated_at,
    )
    assert repository.result(plan.plan_id) == run.result
    with factory() as session:
        production_events_after = int(
            session.scalar(
                select(func.count()).select_from(
                    ResearchExperimentOutcomeEventRow
                )
            )
            or 0
        )
    assert production_events_after == production_events_before

    for table in (
        "chronological_meta_oos_plans",
        "meta_oos_outer_audit_reservations",
        "meta_oos_epoch_arm_audit_records",
        "chronological_meta_oos_results",
    ):
        for operation in ("UPDATE", "DELETE"):
            statement = (
                f"UPDATE {table} SET payload_json=payload_json"
                if operation == "UPDATE"
                else f"DELETE FROM {table}"
            )
            with (
                engine.connect() as connection,
                connection.begin(),
                pytest.raises(DBAPIError, match="append-only"),
            ):
                connection.execute(text(statement))


def test_meta_oos_plan_change_and_budget_reuse_fail_closed(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
) -> None:
    _, _, factory = sqlite_database
    repository = MetaOosRepository(factory)
    plan, contract = _plan()
    assert repository.store_plan(plan, contract)
    changed, changed_contract = _plan(
        contract_version="chronological-meta-oos-thresholds-v2"
    )
    with pytest.raises(
        MetaOosPersistenceError,
        match="plan identity conflict",
    ):
        repository.store_plan(changed, changed_contract)
    changed_seed, changed_seed_contract = _plan(bootstrap_seed=7078)
    with pytest.raises(
        MetaOosPersistenceError,
        match="plan identity conflict",
    ):
        repository.store_plan(changed_seed, changed_seed_contract)

    reservation_created_at = plan.epochs[-1].outcome_available_at
    reservation, _ = repository.reserve_outer_audit(
        plan_id=plan.plan_id,
        idempotency_key="outer-audit-run-1",
        maximum_dataset_uses=1,
        maximum_ttl_hours=24,
        created_at=reservation_created_at,
        expires_at=reservation_created_at + timedelta(hours=24),
    )
    assert reservation.outer_audit_budget_ordinal == 1
    second_plan, second_contract = _plan(
        plan_id="meta-oos-plan-2",
        budget_ordinal=2,
    )
    with pytest.raises(
        MetaOosError,
        match="META_OOS_OUTER_AUDIT_BUDGET_EXCEEDED",
    ):
        repository.store_plan(second_plan, second_contract)


def test_meta_oos_different_reservation_key_for_same_plan_is_rejected(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
) -> None:
    _, _, factory = sqlite_database
    repository = MetaOosRepository(factory)
    plan, contract = _plan()
    repository.store_plan(plan, contract)
    reservation_created_at = plan.epochs[-1].outcome_available_at
    repository.reserve_outer_audit(
        plan_id=plan.plan_id,
        idempotency_key="outer-audit-run-1",
        maximum_dataset_uses=1,
        maximum_ttl_hours=24,
        created_at=reservation_created_at,
        expires_at=reservation_created_at + timedelta(hours=24),
    )
    with pytest.raises(
        MetaOosPersistenceError,
        match="different reservation",
    ):
        repository.reserve_outer_audit(
            plan_id=plan.plan_id,
            idempotency_key="outer-audit-run-changed",
            maximum_dataset_uses=1,
            maximum_ttl_hours=24,
            created_at=reservation_created_at,
            expires_at=reservation_created_at + timedelta(hours=24),
        )


def test_meta_oos_cli_plan_run_and_verify_are_paper_only(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    repository_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url, _, factory = sqlite_database
    plan, contract = _plan()
    plan_file = tmp_path / "meta-oos-plan.json"
    plan_file.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "TRADING_CONFIG_DIR",
        str(repository_root / "config"),
    )
    monkeypatch.setenv("TRADING_RAW_STORE", str(tmp_path / "raw"))
    monkeypatch.setenv(
        "TRADING_PAPER_ACCOUNT_FILE",
        str(repository_root / "config" / "paper-account.example.yaml"),
    )
    runner = CliRunner()

    dry_plan = runner.invoke(
        app,
        ["research", "meta-oos", "plan", "--input", str(plan_file)],
    )
    assert dry_plan.exit_code == 0, dry_plan.output
    assert '"mode": "DRY_RUN"' in dry_plan.stdout
    assert MetaOosRepository(factory).plan(plan.plan_id) is None

    committed_plan = runner.invoke(
        app,
        [
            "research",
            "meta-oos",
            "plan",
            "--input",
            str(plan_file),
            "--commit",
        ],
    )
    assert committed_plan.exit_code == 0, committed_plan.output
    assert '"persisted": true' in committed_plan.stdout.lower()

    dry_run = runner.invoke(
        app,
        ["research", "meta-oos", "run", "--plan-id", plan.plan_id],
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert "DRY_RUN_READY" in dry_run.stdout
    assert '"raw_audit_input_accepted_by_cli": false' in dry_run.stdout

    reservation_created_at = plan.epochs[-1].outcome_available_at
    expires_at = reservation_created_at + timedelta(hours=24)
    committed_run = runner.invoke(
        app,
        [
            "research",
            "meta-oos",
            "run",
            "--plan-id",
            plan.plan_id,
            "--idempotency-key",
            "meta-oos-cli-run",
            "--created-at",
            reservation_created_at.isoformat(),
            "--expires-at",
            expires_at.isoformat(),
            "--commit",
        ],
    )
    assert committed_run.exit_code == 0, committed_run.output
    assert "RESERVED_AWAITING_TRUSTED_ENVIRONMENT" in committed_run.stdout
    repository = MetaOosRepository(factory)
    reservation = repository.reservation(plan.plan_id)
    assert reservation is not None
    run = _run(plan, contract, reservation.reservation_hash)
    repository.store_run(
        run=run,
        reservation=reservation,
        evaluation_contract=contract,
        created_at=run.result.evaluated_at,
    )

    verified = runner.invoke(
        app,
        ["research", "meta-oos", "verify", "--plan-id", plan.plan_id],
    )
    assert verified.exit_code == 0, verified.output
    assert '"verified": true' in verified.stdout.lower()
    assert '"read_only": true' in verified.stdout.lower()
    assert '"real_order_routing": false' in verified.stdout.lower()
