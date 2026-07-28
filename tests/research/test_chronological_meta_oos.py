from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from trading.domain.hashing import canonical_hash
from trading.research.chronological_meta_oos import (
    META_OOS_POLICY_ARMS,
    ChronologicalMetaOosPlanV1,
    DeterministicSyntheticMetaOosEnvironment,
    FixedRecalibrationPolicyAdapter,
    MetaOosAuditMode,
    MetaOosBudgetV1,
    MetaOosCandidateAvailabilityV1,
    MetaOosDecisionKind,
    MetaOosEpochContextV1,
    MetaOosEpochV1,
    MetaOosError,
    MetaOosEvaluationContractV1,
    MetaOosLearningOutcomeV1,
    MetaOosMemorySnapshotV1,
    MetaOosPolicyArm,
    MetaOosPolicyDecisionV1,
    ResearchPolicyAdapter,
    StaticChampionPolicyAdapter,
    SyntheticPolicyAdapter,
    build_chronological_meta_oos_plan,
    build_meta_oos_commander_binding,
    build_meta_oos_commander_invocation,
    build_meta_oos_evaluation_contract,
    build_meta_oos_memory_snapshot,
    build_meta_oos_policy_decision,
    run_chronological_meta_oos,
    verify_chronological_meta_oos_result,
)
from trading.research.experiment_outcomes import ExperimentInformationRole
from trading.research.portfolio_delta_sharpe import (
    StationaryBootstrapContractV1,
)

BASE = datetime(2020, 1, 1, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _evaluation_contract(
    *,
    maximum_epochs: int = 52,
) -> MetaOosEvaluationContractV1:
    return build_meta_oos_evaluation_contract(
        contract_version="chronological-meta-oos-thresholds-v1",
        annualization_sessions=252,
        minimum_epochs=8,
        maximum_epochs=maximum_epochs,
        maximum_candidate_generation_budget_per_epoch=10,
        maximum_oos_budget_per_epoch=3,
        maximum_outer_audit_uses_per_dataset=1,
        reservation_ttl_hours=24,
        minimum_adaptive_delta_sharpe_lcb=0.0,
        minimum_research_efficiency=0.0,
        maximum_allowed_drawdown=0.40,
        tail_quantile=0.05,
        maximum_absolute_daily_return=1.0,
    )


def _epochs(count: int = 12) -> tuple[MetaOosEpochV1, ...]:
    values: list[MetaOosEpochV1] = []
    for index in range(count):
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
                market_data_manifest_hash=canonical_hash(
                    {"epoch": index}
                ),
                context_key="REGIME_X" if index % 2 == 0 else "REGIME_Y",
                candidate_generation_budget=2,
                oos_budget=1,
            )
        )
    return tuple(values)


def _plan(
    *,
    evaluation_contract: MetaOosEvaluationContractV1 | None = None,
    epochs: tuple[MetaOosEpochV1, ...] | None = None,
) -> ChronologicalMetaOosPlanV1:
    contract = evaluation_contract or _evaluation_contract()
    commander = build_meta_oos_commander_binding(
        model_family="GPT-5.6-SOL",
        model_version="gpt-5.6-sol",
        reasoning_profile="max",
        prompt_template_hash=HASH_A,
        request_schema_hash=HASH_B,
        output_schema_hash=HASH_C,
    )
    return build_chronological_meta_oos_plan(
        plan_id="meta-oos-plan-1",
        plan_version="chronological-meta-oos-v1",
        initial_champion_manifest_hash=HASH_D,
        epochs=epochs or _epochs(),
        policy_adapter_versions={
            MetaOosPolicyArm.STATIC_CHAMPION: "static-champion-v1",
            MetaOosPolicyArm.FIXED_RECALIBRATION: (
                "fixed-recalibration-v1"
            ),
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
        cost_model_hash=HASH_A,
        execution_model_hash=HASH_B,
        bootstrap_contract=StationaryBootstrapContractV1(
            configured_seed=7077,
            samples=300,
            expected_block_sessions=5,
            lower_quantile=0.025,
            variance_epsilon=1e-12,
        ),
        evaluation_contract_hash=contract.contract_hash,
        outer_audit_dataset_id="synthetic-outer-audit-v1",
        outer_audit_budget_ordinal=1,
        created_at=BASE - timedelta(days=1),
    )


def _adapters() -> dict[MetaOosPolicyArm, ResearchPolicyAdapter]:
    synthetic_memoryless = SyntheticPolicyAdapter(("ACTION_A", "ACTION_B"))
    synthetic_adaptive = SyntheticPolicyAdapter(("ACTION_A", "ACTION_B"))
    return {
        MetaOosPolicyArm.STATIC_CHAMPION: StaticChampionPolicyAdapter(),
        MetaOosPolicyArm.FIXED_RECALIBRATION: (
            FixedRecalibrationPolicyAdapter(
                {epoch.epoch_id: "ACTION_A" for epoch in _epochs()}
            )
        ),
        MetaOosPolicyArm.MEMORYLESS_COMMANDER: synthetic_memoryless,
        MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER: synthetic_adaptive,
    }


def _run(
    *,
    relationship: Mapping[str, str],
    environment: DeterministicSyntheticMetaOosEnvironment | None = None,
):
    contract = _evaluation_contract()
    plan = _plan(evaluation_contract=contract)
    return run_chronological_meta_oos(
        plan=plan,
        evaluation_contract=contract,
        adapters=_adapters(),
        environment=(
            environment
            or DeterministicSyntheticMetaOosEnvironment(
                context_action_edge=relationship,
            )
        ),
        outer_audit_reservation_hash="e" * 64,
        evaluated_at=plan.epochs[-1].outcome_available_at,
    )


def test_adaptive_arm_learns_synthetic_context_action_relationship() -> None:
    run = _run(
        relationship={
            "REGIME_X": "ACTION_A",
            "REGIME_Y": "ACTION_B",
        }
    )
    result = run.result
    adaptive = next(
        item
        for item in result.arm_results
        if item.arm is MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER
    )
    memoryless = next(
        item
        for item in result.arm_results
        if item.arm is MetaOosPolicyArm.MEMORYLESS_COMMANDER
    )
    comparison = next(
        item
        for item in result.paired_comparisons
        if item.candidate_arm
        is MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER
        and item.baseline_arm
        is MetaOosPolicyArm.MEMORYLESS_COMMANDER
    )
    assert adaptive.portfolio_sharpe > memoryless.portfolio_sharpe
    assert comparison.delta_sharpe_point > 0
    assert comparison.delta_sharpe_lcb > 0
    assert result.adaptive_system_pass
    assert tuple(item.arm for item in result.arm_results) == (
        META_OOS_POLICY_ARMS
    )


def test_unrelated_synthetic_environment_creates_no_false_improvement() -> None:
    run = _run(relationship={})
    adaptive_comparisons = tuple(
        item
        for item in run.result.paired_comparisons
        if item.candidate_arm
        is MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER
    )
    assert all(abs(item.delta_sharpe_point) <= 1e-12 for item in adaptive_comparisons)
    assert not run.result.adaptive_system_pass


def test_same_plan_seed_and_inputs_replay_identically() -> None:
    first = _run(
        relationship={
            "REGIME_X": "ACTION_A",
            "REGIME_Y": "ACTION_B",
        }
    )
    second = _run(
        relationship={
            "REGIME_X": "ACTION_A",
            "REGIME_Y": "ACTION_B",
        }
    )
    assert first.result.result_hash == second.result.result_hash
    assert tuple(item.record_hash for item in first.audit_records) == tuple(
        item.record_hash for item in second.audit_records
    )
    verify_chronological_meta_oos_result(
        plan=_plan(),
        evaluation_contract=_evaluation_contract(),
        result=first.result,
    )


def test_verify_recomputes_the_adaptive_verdict() -> None:
    run = _run(relationship={})
    payload = run.result.model_dump(
        mode="python",
        exclude={"result_hash"},
    )
    payload["adaptive_system_pass"] = True
    payload["reason_codes"] = ()
    tampered = type(run.result).model_validate(
        {**payload, "result_hash": canonical_hash(payload)}
    )
    with pytest.raises(
        MetaOosError,
        match="META_OOS_RESULT_VERDICT_MISMATCH",
    ):
        verify_chronological_meta_oos_result(
            plan=_plan(),
            evaluation_contract=_evaluation_contract(),
            result=tampered,
        )


def test_runtime_enforces_epoch_limit_and_distinct_arm_state() -> None:
    bounded_contract = _evaluation_contract(maximum_epochs=8)
    bounded_plan = _plan(evaluation_contract=bounded_contract)
    with pytest.raises(MetaOosError, match="META_OOS_TOO_MANY_EPOCHS"):
        run_chronological_meta_oos(
            plan=bounded_plan,
            evaluation_contract=bounded_contract,
            adapters=_adapters(),
            environment=DeterministicSyntheticMetaOosEnvironment(
                context_action_edge={}
            ),
            outer_audit_reservation_hash="e" * 64,
            evaluated_at=bounded_plan.epochs[-1].outcome_available_at,
        )

    contract = _evaluation_contract()
    plan = _plan(evaluation_contract=contract)
    adapters = _adapters()
    adapters[MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER] = adapters[
        MetaOosPolicyArm.MEMORYLESS_COMMANDER
    ]
    with pytest.raises(
        MetaOosError,
        match="META_OOS_POLICY_ADAPTER_STATE_SHARED",
    ):
        run_chronological_meta_oos(
            plan=plan,
            evaluation_contract=contract,
            adapters=adapters,
            environment=DeterministicSyntheticMetaOosEnvironment(
                context_action_edge={}
            ),
            outer_audit_reservation_hash="e" * 64,
            evaluated_at=plan.epochs[-1].outcome_available_at,
        )


class _FutureDataEnvironment(DeterministicSyntheticMetaOosEnvironment):
    def execute_epoch(self, **kwargs):
        private = super().execute_epoch(**kwargs)
        if private.epoch_id != "epoch-03":
            return private
        observations = list(private.observations)
        observations[0] = replace(
            observations[0],
            available_at=kwargs["epoch"].outcome_available_at
            + timedelta(seconds=1),
        )
        return replace(private, observations=tuple(observations))


def test_one_future_available_at_record_fails_closed() -> None:
    with pytest.raises(MetaOosError, match="META_OOS_PRIVATE_PIT_VIOLATION"):
        _run(
            relationship={"REGIME_X": "ACTION_A"},
            environment=_FutureDataEnvironment(
                context_action_edge={"REGIME_X": "ACTION_A"}
            ),
        )


def _learning_outcome(
    *,
    epoch_id: str,
    role: ExperimentInformationRole,
    available_at: datetime,
    matured: bool = True,
) -> MetaOosLearningOutcomeV1:
    payload = {
        "schema_version": "meta_oos_learning_outcome_v1",
        "outcome_id": f"outcome-{epoch_id}-{role.value}",
        "arm": MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER,
        "epoch_id": epoch_id,
        "context_key": "REGIME_X",
        "action_kind": "ACTION_A",
        "information_role": role,
        "available_at": available_at,
        "matured": matured,
        "reward": 0.1,
        "technical_failure": False,
    }
    return MetaOosLearningOutcomeV1.model_validate(
        {**payload, "outcome_hash": canonical_hash(payload)}
    )


def test_adaptive_memory_uses_only_prior_matured_learning_forward() -> None:
    cutoff = BASE + timedelta(days=100)
    eligible = _learning_outcome(
        epoch_id="eligible",
        role=ExperimentInformationRole.LEARNING_FORWARD,
        available_at=cutoff - timedelta(days=1),
    )
    snapshot: MetaOosMemorySnapshotV1 = build_meta_oos_memory_snapshot(
        outcomes=(
            eligible,
            _learning_outcome(
                epoch_id="future",
                role=ExperimentInformationRole.LEARNING_FORWARD,
                available_at=cutoff + timedelta(seconds=1),
            ),
            _learning_outcome(
                epoch_id="unmatured",
                role=ExperimentInformationRole.LEARNING_FORWARD,
                available_at=cutoff - timedelta(days=1),
                matured=False,
            ),
            _learning_outcome(
                epoch_id="oos",
                role=ExperimentInformationRole.PROMOTION_OOS,
                available_at=cutoff - timedelta(days=1),
            ),
            _learning_outcome(
                epoch_id="audit",
                role=ExperimentInformationRole.META_AUDIT,
                available_at=cutoff - timedelta(days=1),
            ),
        ),
        as_of=cutoff,
    )
    assert snapshot.outcome_hashes == (eligible.outcome_hash,)


class _FutureCandidateAdapter:
    adapter_version = "synthetic-context-learning-v1"

    def plan_research(
        self,
        *,
        epoch_context: MetaOosEpochContextV1,
        research_memory_snapshot: MetaOosMemorySnapshotV1 | None,
        budget: MetaOosBudgetV1,
    ) -> MetaOosPolicyDecisionV1:
        del budget
        return build_meta_oos_policy_decision(
            epoch_context=epoch_context,
            decision_kind=MetaOosDecisionKind.RUN_ACTION,
            action_kind="ACTION_A",
            candidate_id="future-candidate",
            research_memory_snapshot_hash=(
                None
                if research_memory_snapshot is None
                else research_memory_snapshot.snapshot_hash
            ),
            commander_invocation=None,
            predicted_reward=None,
        )


class _FutureInvocationAdapter:
    adapter_version = "synthetic-context-learning-v1"

    def __init__(self, plan: ChronologicalMetaOosPlanV1) -> None:
        self._binding = plan.commander_binding

    def plan_research(
        self,
        *,
        epoch_context: MetaOosEpochContextV1,
        research_memory_snapshot: MetaOosMemorySnapshotV1 | None,
        budget: MetaOosBudgetV1,
    ) -> MetaOosPolicyDecisionV1:
        del budget
        invocation = build_meta_oos_commander_invocation(
            commander_binding=self._binding,
            prompt_hash=HASH_A,
            request_hash=HASH_B,
            output_hash=HASH_C,
            invoked_at=epoch_context.decision_at + timedelta(seconds=1),
        )
        return build_meta_oos_policy_decision(
            epoch_context=epoch_context,
            decision_kind=MetaOosDecisionKind.RUN_ACTION,
            action_kind="ACTION_A",
            candidate_id=None,
            research_memory_snapshot_hash=(
                None
                if research_memory_snapshot is None
                else research_memory_snapshot.snapshot_hash
            ),
            commander_invocation=invocation,
            predicted_reward=None,
        )


def test_later_candidate_cannot_be_backdated_into_epoch() -> None:
    contract = _evaluation_contract()
    plan = _plan(evaluation_contract=contract)
    candidate_payload = {
        "schema_version": "meta_oos_candidate_availability_v1",
        "candidate_id": "future-candidate",
        "candidate_artifact_hash": HASH_A,
        "first_available_at": plan.epochs[0].decision_at
        + timedelta(seconds=1),
        "proposal_created_at": plan.epochs[0].decision_at,
        "source_available_at": (plan.epochs[0].decision_at,),
    }
    candidate = MetaOosCandidateAvailabilityV1.model_validate(
        {
            **candidate_payload,
            "availability_hash": canonical_hash(candidate_payload),
        }
    )
    adapters = _adapters()
    adapters[MetaOosPolicyArm.MEMORYLESS_COMMANDER] = (
        _FutureCandidateAdapter()
    )
    with pytest.raises(
        MetaOosError,
        match="META_OOS_CANDIDATE_NOT_POINT_IN_TIME",
    ):
        run_chronological_meta_oos(
            plan=plan,
            evaluation_contract=contract,
            adapters=adapters,
            environment=DeterministicSyntheticMetaOosEnvironment(
                context_action_edge={}
            ),
            outer_audit_reservation_hash="e" * 64,
            candidate_catalog={candidate.candidate_id: candidate},
            evaluated_at=plan.epochs[-1].outcome_available_at,
        )


def test_commander_invocation_after_decision_fails_closed() -> None:
    contract = _evaluation_contract()
    plan = _plan(evaluation_contract=contract)
    adapters = _adapters()
    adapters[MetaOosPolicyArm.MEMORYLESS_COMMANDER] = (
        _FutureInvocationAdapter(plan)
    )
    with pytest.raises(
        MetaOosError,
        match="META_OOS_COMMANDER_INVOCATION_MISMATCH",
    ):
        run_chronological_meta_oos(
            plan=plan,
            evaluation_contract=contract,
            adapters=adapters,
            environment=DeterministicSyntheticMetaOosEnvironment(
                context_action_edge={}
            ),
            outer_audit_reservation_hash="e" * 64,
            evaluated_at=plan.epochs[-1].outcome_available_at,
        )


def test_result_is_aggregate_only_and_arm_state_is_separate() -> None:
    run = _run(
        relationship={
            "REGIME_X": "ACTION_A",
            "REGIME_Y": "ACTION_B",
        }
    )
    serialized = json.dumps(run.result.model_dump(mode="json"))
    for forbidden in (
        "daily_returns",
        "session_key",
        "bootstrap_samples",
        "private_observation",
        "trade_returns",
    ):
        assert forbidden not in serialized
    assert all(
        item.memory_snapshot_hash is None
        for item in run.audit_records
        if item.arm is not MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER
    )
    assert all(
        item.memory_snapshot_hash is not None
        for item in run.audit_records
        if item.arm is MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER
    )
