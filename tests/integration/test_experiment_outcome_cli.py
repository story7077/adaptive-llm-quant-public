from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from trading.cli import app
from trading.domain.hashing import canonical_hash, stable_id
from trading.persistence.experiment_outcomes import ExperimentOutcomeRepository
from trading.research.experiment_outcomes import (
    ExperimentInformationRole,
    ExperimentMaturityStatus,
    ExperimentOutcomeEventKind,
    ExperimentOutcomeMaturationInputV1,
    ExperimentStage,
    ResearchActionKind,
    ResearchExperimentActionV1,
)

NOW = datetime(2026, 7, 28, 0, 0, tzinfo=UTC)
HASH_A = "a" * 64


def _registered_action() -> ResearchExperimentActionV1:
    payload = {
        "schema_version": "research_experiment_action_v1",
        "action_id": stable_id("research-experiment-action", "cli-experiment"),
        "experiment_id": "cli-experiment",
        "research_cycle_id": "cli-cycle",
        "proposal_id": "cli-proposal",
        "challenger_id": "cli-challenger",
        "parent_strategy_id": "parent",
        "parent_strategy_version": "1.0.0",
        "candidate_strategy_version": "1.1.0",
        "primary_action_kind": ResearchActionKind.ADD_FEATURE,
        "secondary_action_kinds": (),
        "mechanism_tags": ("cli",),
        "information_role": ExperimentInformationRole.LEARNING_FORWARD,
        "decision_at": NOW,
        "maturity_due_at": NOW,
        "predicted_delta_sharpe_lower": None,
        "predicted_delta_sharpe_median": None,
        "predicted_delta_sharpe_upper": None,
        "predicted_failure_codes": (),
        "complexity_delta": 0.0,
        "candidate_artifact_hash": HASH_A,
        "evaluation_contract_hash": HASH_A,
        "source_artifact_hashes": (),
        "source_data_available_at": (),
        "legacy_proposal": False,
        "meta_training_permitted": True,
        "idempotency_key": "cli-action",
        "created_at": NOW,
    }
    return ResearchExperimentActionV1.model_validate(
        {**payload, "action_hash": canonical_hash(payload)}
    )


def test_outcome_and_memory_cli_support_dry_run_and_commit(
    sqlite_database,
    repository_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    repository.register_action(_registered_action())
    maturation = ExperimentOutcomeMaturationInputV1(
        experiment_id="cli-experiment",
        event_kind=ExperimentOutcomeEventKind.TECHNICAL_OUTCOME_RECORDED,
        experiment_stage=ExperimentStage.BUILD,
        available_at=NOW,
        maturity_status=ExperimentMaturityStatus.MATURED,
        technical_success=False,
        technical_failure_codes=("BUILD_FAILED",),
        evaluation_contract_hash=HASH_A,
        source_artifact_hashes=(),
        source_data_available_at=(),
        idempotency_key="cli-build-failure",
        created_at=NOW,
    )
    input_file = tmp_path / "maturation.json"
    input_file.write_text(maturation.model_dump_json(indent=2), encoding="utf-8")
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

    dry = runner.invoke(
        app,
        ["research", "outcome", "mature", "--input", str(input_file)],
    )
    assert dry.exit_code == 0, dry.output
    assert '"mode": "DRY_RUN"' in dry.stdout
    assert '"real_order_routing": false' in dry.stdout
    assert repository.event_chain("cli-experiment") == ()

    committed = runner.invoke(
        app,
        [
            "research",
            "outcome",
            "mature",
            "--input",
            str(input_file),
            "--commit",
        ],
    )
    assert committed.exit_code == 0, committed.output
    assert '"created": true' in committed.stdout.lower()
    assert len(repository.event_chain("cli-experiment")) == 1

    memory_dry = runner.invoke(
        app,
        [
            "research",
            "memory",
            "materialize",
            "--as-of",
            NOW.isoformat(),
            "--data-available-cutoff",
            NOW.isoformat(),
            "--created-at",
            NOW.isoformat(),
        ],
    )
    assert memory_dry.exit_code == 0, memory_dry.output
    assert '"persisted": false' in memory_dry.stdout.lower()
    memory_commit = runner.invoke(
        app,
        [
            "research",
            "memory",
            "materialize",
            "--as-of",
            NOW.isoformat(),
            "--data-available-cutoff",
            NOW.isoformat(),
            "--created-at",
            NOW.isoformat(),
            "--commit",
        ],
    )
    assert memory_commit.exit_code == 0, memory_commit.output
    assert '"persisted": true' in memory_commit.stdout.lower()
    snapshot_id = json.loads(memory_commit.stdout)["snapshot"]["snapshot_id"]

    meta_arguments = [
        "research",
        "meta-policy",
        "build",
        "--snapshot-id",
        snapshot_id,
        "--research-cycle-id",
        "cli-meta-cycle",
        "--regime-cluster-id",
        "regime-neutral",
        "--failure-cluster-id",
        "failure-build",
        "--portfolio-exposure-cluster-id",
        "exposure-balanced",
        "--maximum-total-submissions",
        "2",
        "--idempotency-key",
        "cli-meta-plan",
        "--generated-at",
        NOW.isoformat(),
    ]
    meta_dry = runner.invoke(app, meta_arguments)
    assert meta_dry.exit_code == 0, meta_dry.output
    assert json.loads(meta_dry.stdout)["persisted"] is False
    meta_commit = runner.invoke(app, [*meta_arguments, "--commit"])
    assert meta_commit.exit_code == 0, meta_commit.output
    meta_payload = json.loads(meta_commit.stdout)
    assert meta_payload["persisted"] is True
    assert meta_payload["automatic_promotion_enabled"] is False
    assert meta_payload["real_order_routing"] is False

    status = runner.invoke(app, ["research", "status"])
    assert status.exit_code == 0, status.output
    payload = json.loads(status.stdout)
    recursive = payload["recursive_improvement"]
    assert recursive["status"] == "DISABLED_RESEARCH_ONLY_PR3"
    assert recursive["portfolio_delta_sharpe"]["status"] == (
        "IMPLEMENTED_DISABLED"
    )
    assert recursive["enabled"] is False
    assert recursive["candidate_patch_policy"]["version"] == (
        "candidate_patch_policy_v2"
    )
    assert recursive["experiment_outcome_ledger"] == (
        payload["experiment_outcome_ledger"]
    )
    assert recursive["meta_controller"]["action_plan_count"] == 1
    assert recursive["meta_controller"]["automatic_execution_enabled"] is False
    assert recursive["experiment_outcome_ledger"][
        "effective_unsuperseded_event_count"
    ] == 1
    assert recursive["real_order_routing"] is False
