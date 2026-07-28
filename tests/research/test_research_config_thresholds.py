from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.domain.hashing import canonical_hash
from trading.research.config import (
    candidate_execution_security,
    candidate_process_limits,
    load_research_config,
    oos_process_evaluation_config,
    recursive_improvement_status,
    shadow_paper_parameters,
)
from trading.research.sandbox_contract import (
    CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH,
)


def test_operational_falsification_thresholds_are_versioned_and_hashed() -> None:
    config_dir = Path(__file__).resolve().parents[2] / "config"
    bundle = load_research_config(config_dir)
    contract = bundle.config.falsification.evaluation_contract

    assert contract.contract_version == "research-falsification-thresholds-v1"
    assert contract.minimum_session_count == 126
    assert contract.cost_stress_multipliers == (1.0, 2.0, 3.0)
    changed_contract = contract.model_copy(
        update={"minimum_session_count": contract.minimum_session_count + 1}
    )
    changed_falsification = bundle.config.falsification.model_copy(
        update={"evaluation_contract": changed_contract}
    )
    changed_config = bundle.config.model_copy(
        update={"falsification": changed_falsification}
    )
    assert canonical_hash(changed_config) != canonical_hash(bundle.config)
    assert len(bundle.manifest_hash) == 64


def test_oos_and_shadow_runtime_numerics_are_versioned_and_hashed() -> None:
    config_dir = Path(__file__).resolve().parents[2] / "config"
    bundle = load_research_config(config_dir)
    cutoff = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)

    oos = oos_process_evaluation_config(
        bundle,
        dataset_id="locked-oos-v1",
        dataset_manifest_hash="a" * 64,
        data_available_cutoff=cutoff,
        expected_source_data_manifest_hash="b" * 64,
        expected_candidate_replay_hash="c" * 64,
    )
    assert oos.annualization_sessions == 252
    assert oos.newey_west_lag == 5
    assert oos.bootstrap_seed == 7077
    assert oos.bootstrap_block_length == 10
    assert oos.bootstrap_samples == 2000
    assert oos.base_cost_bps == 10
    assert oos.request_ttl_seconds == 900
    assert oos.worker_timeout_seconds == 60
    assert oos.cost_sensitivity_bps == (0, 5, 10)
    assert oos.expected_source_data_manifest_hash == "b" * 64
    assert oos.expected_candidate_replay_hash == "c" * 64
    assert (
        oos.expected_trusted_producer_version
        == "trusted_candidate_evaluation_v1"
    )

    shadow = shadow_paper_parameters(bundle)
    assert shadow.contract_version == "shadow-paper-v1"
    assert shadow.commission_rate == Decimal("0.001")
    assert shadow.displayed_participation_rate == Decimal("0.10")
    assert shadow.maximum_quote_age_seconds == 15
    assert shadow.real_order_routing is False

    changed_oos = bundle.config.oos.model_copy(
        update={"bootstrap_seed": bundle.config.oos.bootstrap_seed + 1}
    )
    changed_config = bundle.config.model_copy(update={"oos": changed_oos})
    assert canonical_hash(changed_config) != canonical_hash(bundle.config)


def test_candidate_execution_limits_and_isolation_are_versioned() -> None:
    config_dir = Path(__file__).resolve().parents[2] / "config"
    bundle = load_research_config(config_dir)

    limits = candidate_process_limits(bundle)
    assert limits.timeout_seconds == 10
    assert limits.maximum_stdout_bytes == 32768
    assert limits.maximum_stderr_bytes == 8192
    assert limits.maximum_memory_bytes == 268435456
    assert limits.maximum_processes == 4

    security = candidate_execution_security(
        bundle,
        candidate_artifact_hash="a" * 64,
        candidate_tree_hash="b" * 64,
        runtime_executable_hash="c" * 64,
        worker_code_hash="d" * 64,
        declared_entrypoint="candidate.entrypoint:evaluate",
    )
    assert security.isolation_kind == "CODEX_WINDOWS_RESTRICTED_TOKEN"
    assert security.isolation_version == "1.0.0"
    assert security.limits == limits
    assert security.real_order_routing is False

    changed_execution = bundle.config.candidate_execution.model_copy(
        update={
            "maximum_memory_bytes": (
                bundle.config.candidate_execution.maximum_memory_bytes + 1
            )
        }
    )
    changed_config = bundle.config.model_copy(
        update={"candidate_execution": changed_execution}
    )
    assert canonical_hash(changed_config) != canonical_hash(bundle.config)


def test_recursive_improvement_is_disabled_and_bound_to_patch_policy_v2() -> None:
    config_dir = Path(__file__).resolve().parents[2] / "config"
    bundle = load_research_config(config_dir)
    recursive = bundle.config.recursive_improvement

    assert recursive.enabled is False
    assert recursive.meta_oos.enabled is False
    assert recursive.candidate_patch_policy_version == "candidate_patch_policy_v2"
    assert (
        recursive.candidate_patch_policy_hash
        == CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH
    )
    assert recursive.outcome_ledger.learning_forward_horizon_sessions == 63
    status = recursive_improvement_status(
        bundle,
        experiment_outcome_ledger={
            "action_count": 0,
            "event_count": 0,
            "effective_unsuperseded_event_count": 0,
            "effective_eligible_learning_forward_event_count": 0,
            "snapshot_count": 0,
        },
    )
    assert status["status"] == "DISABLED_RESEARCH_ONLY_PR2"
    assert status["enabled"] is False
    assert status["audit_only"] is True
    assert status["candidate_patch_policy"] == {
        "version": "candidate_patch_policy_v2",
        "contract_hash": CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH,
    }
    assert status["automatic_outcome_maintenance_enabled"] is False
    assert status["automatic_promotion_enabled"] is False
    assert status["real_order_routing"] is False
