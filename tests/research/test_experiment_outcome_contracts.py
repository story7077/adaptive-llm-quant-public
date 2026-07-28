from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.domain.hashing import canonical_hash
from trading.research.contracts import AlgorithmProposalV1
from trading.research.experiment_outcomes import (
    AlgorithmProposalV2,
    ExperimentInformationRole,
    ExperimentMaturityStatus,
    ExperimentOutcomeEventKind,
    ExperimentOutcomeMaturationInputV1,
    ExperimentStage,
    PredictedPortfolioDeltaSharpeV1,
    ResearchActionKind,
    ResearchExperimentActionV1,
    build_experiment_action,
    build_outcome_event,
)

NOW = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
DUE = NOW + timedelta(days=2)
HASH_A = "a" * 64
HASH_B = "b" * 64


def proposal_v2(
    *,
    proposal_id: str = "proposal-v2",
    action_kind: ResearchActionKind = ResearchActionKind.ADD_FEATURE,
) -> AlgorithmProposalV2:
    payload = {
        "schema_version": "algorithm_proposal_v2",
        "proposal_id": proposal_id,
        "hypothesis_id": f"hypothesis-{proposal_id}",
        "hypothesis": "A point-in-time feature may diversify the parent.",
        "economic_mechanism": "Independent information may reduce correlated errors.",
        "why_current_model_failed": "The parent omitted the declared feature.",
        "parent_strategy_id": "parent",
        "parent_strategy_version": "1.0.0",
        "proposed_strategy_id": "candidate",
        "proposed_strategy_version": "1.1.0",
        "target_horizon": "DAILY",
        "target_universe": ["QQQ"],
        "required_data": ["adjusted_daily_bars"],
        "feature_changes": ["add declared feature"],
        "signal_formula_changes": [],
        "entry_rule_changes": [],
        "exit_rule_changes": [],
        "position_sizing_changes": [],
        "regime_activation_changes": [],
        "calibration_changes": [],
        "expected_edge_source": "Diversifying point-in-time signal.",
        "expected_failure_modes": ["NO_EDGE"],
        "invalidation_conditions": ["No forward delta."],
        "placebo_tests": ["date_shift"],
        "stress_tests": ["cost_3x"],
        "minimum_economic_effect": {"delta_sharpe": 0.01},
        "estimated_capacity": {"usd": 100000},
        "estimated_turnover": {"annualized": 2.0},
        "estimated_cost_sensitivity": {"bps": 10},
        "files_allowed_to_change": [
            "src/trading/strategies/challengers/candidate/**",
            "tests/candidates/test_candidate.py",
        ],
        "tests_required": ["tests/candidates/test_candidate.py"],
        "evidence_source_ids": ["source-1"],
        "raw_confidence": 0.5,
        "patch_policy_version": "candidate_patch_policy_v2",
        "primary_action_kind": action_kind,
        "secondary_action_kinds": (),
        "mechanism_tags": ("diversification",),
        "predicted_portfolio_delta_sharpe": {
            "lower": -0.1,
            "median": 0.1,
            "upper": 0.3,
        },
        "predicted_failure_codes": ("NO_EDGE",),
        "complexity_delta": 1.0,
    }
    return AlgorithmProposalV2.model_validate(
        {**payload, "proposal_hash": canonical_hash(payload)}
    )


def proposal_v1() -> AlgorithmProposalV1:
    payload = proposal_v2().model_dump(
        mode="python",
        exclude={
            "proposal_hash",
            "patch_policy_version",
            "primary_action_kind",
            "secondary_action_kinds",
            "mechanism_tags",
            "predicted_portfolio_delta_sharpe",
            "predicted_failure_codes",
            "complexity_delta",
        },
    )
    payload["schema_version"] = "algorithm_proposal_v1"
    return AlgorithmProposalV1.model_validate(
        {**payload, "proposal_hash": canonical_hash(payload)}
    )


def action(
    *,
    role: ExperimentInformationRole = ExperimentInformationRole.LEARNING_FORWARD,
):
    return build_experiment_action(
        proposal=proposal_v2(),
        experiment_id=f"experiment-{role.value.lower()}",
        research_cycle_id="cycle-1",
        challenger_id="challenger-1",
        information_role=role,
        decision_at=NOW,
        maturity_due_at=DUE,
        candidate_artifact_hash=HASH_A,
        evaluation_contract_hash=HASH_B,
        source_artifact_hashes=(HASH_A,),
        source_data_available_at=(NOW,),
        idempotency_key=f"action-{role.value.lower()}",
        created_at=NOW,
    )


def matured_input(
    *,
    role_suffix: str = "learning_forward",
    idempotency_key: str = "mature-1",
) -> ExperimentOutcomeMaturationInputV1:
    return ExperimentOutcomeMaturationInputV1(
        experiment_id=f"experiment-{role_suffix}",
        event_kind=ExperimentOutcomeEventKind.ECONOMIC_OUTCOME_MATURED,
        experiment_stage=ExperimentStage.FORWARD,
        evaluation_window_start=NOW,
        evaluation_window_end=DUE,
        available_at=DUE,
        maturity_status=ExperimentMaturityStatus.MATURED,
        technical_success=True,
        technical_failure_codes=(),
        portfolio_delta_sharpe_point=0.2,
        portfolio_delta_sharpe_lcb=0.05,
        portfolio_delta_sharpe_ucb=0.35,
        worst_cost_delta_sharpe_lcb=0.01,
        drawdown_delta=-0.01,
        tail_loss_delta=-0.005,
        turnover_delta=0.1,
        cost_delta_bps=2.0,
        evaluation_contract_hash=HASH_B,
        source_artifact_hashes=(HASH_A,),
        source_data_available_at=(DUE,),
        idempotency_key=idempotency_key,
        created_at=DUE,
    )


def test_algorithm_proposal_v2_requires_one_typed_primary_action() -> None:
    proposal = proposal_v2()
    assert proposal.primary_action_kind is ResearchActionKind.ADD_FEATURE
    assert isinstance(
        proposal.predicted_portfolio_delta_sharpe,
        PredictedPortfolioDeltaSharpeV1,
    )
    payload = proposal.model_dump(mode="python", exclude={"proposal_hash"})
    payload["primary_action_kind"] = ResearchActionKind.UNKNOWN_LEGACY
    with pytest.raises(ValueError, match="typed primary"):
        AlgorithmProposalV2.model_validate(
            {**payload, "proposal_hash": canonical_hash(payload)}
        )


def test_v1_proposal_maps_only_to_unknown_legacy_and_is_not_trainable() -> None:
    legacy = build_experiment_action(
        proposal=proposal_v1(),
        experiment_id="experiment-legacy",
        research_cycle_id="cycle-1",
        challenger_id="challenger-legacy",
        information_role=ExperimentInformationRole.LEARNING_FORWARD,
        decision_at=NOW,
        maturity_due_at=DUE,
        candidate_artifact_hash=None,
        evaluation_contract_hash=HASH_B,
        source_artifact_hashes=(),
        source_data_available_at=(),
        idempotency_key="action-legacy",
        created_at=NOW,
    )
    assert legacy.primary_action_kind is ResearchActionKind.UNKNOWN_LEGACY
    assert legacy.legacy_proposal is True
    assert legacy.meta_training_permitted is False


def test_economic_metrics_are_forbidden_before_maturity() -> None:
    payload = matured_input().model_dump(mode="python")
    payload["maturity_status"] = ExperimentMaturityStatus.PENDING
    with pytest.raises(ValueError, match="must remain None"):
        ExperimentOutcomeMaturationInputV1.model_validate(payload)


def test_technical_failure_can_mature_immediately_without_economic_reward() -> None:
    registered = action()
    immediate = ExperimentOutcomeMaturationInputV1(
        experiment_id=registered.experiment_id,
        event_kind=ExperimentOutcomeEventKind.TECHNICAL_OUTCOME_RECORDED,
        experiment_stage=ExperimentStage.BUILD,
        available_at=NOW,
        maturity_status=ExperimentMaturityStatus.MATURED,
        technical_success=False,
        technical_failure_codes=("BUILD_FAILED",),
        evaluation_contract_hash=HASH_B,
        source_artifact_hashes=(),
        source_data_available_at=(),
        idempotency_key="build-failed",
        created_at=NOW,
    )
    event = build_outcome_event(
        action=registered,
        maturation=immediate,
        previous_event=None,
    )
    assert event.eligible_for_meta_training is False
    assert event.portfolio_delta_sharpe_point is None


@pytest.mark.parametrize(
    "role",
    (
        ExperimentInformationRole.DISCOVERY,
        ExperimentInformationRole.PROMOTION_OOS,
        ExperimentInformationRole.META_AUDIT,
    ),
)
def test_only_learning_forward_economics_are_training_eligible(
    role: ExperimentInformationRole,
) -> None:
    registered = action(role=role)
    event = build_outcome_event(
        action=registered,
        maturation=matured_input(role_suffix=role.value.lower()),
        previous_event=None,
    )
    assert event.eligible_for_meta_training is False


def test_learning_forward_matured_economic_event_is_training_eligible() -> None:
    event = build_outcome_event(
        action=action(),
        maturation=matured_input(),
        previous_event=None,
    )
    assert event.eligible_for_meta_training is True
    assert event.prediction_error == pytest.approx(0.1)


def test_source_availability_after_outcome_is_rejected() -> None:
    payload = matured_input().model_dump(mode="python")
    payload["source_data_available_at"] = (DUE + timedelta(seconds=1),)
    with pytest.raises(ValueError, match="unavailable"):
        ExperimentOutcomeMaturationInputV1.model_validate(payload)


def test_technical_event_cannot_smuggle_economic_reward() -> None:
    payload = matured_input().model_dump(mode="python")
    payload["event_kind"] = (
        ExperimentOutcomeEventKind.TECHNICAL_OUTCOME_RECORDED
    )
    with pytest.raises(ValueError, match="mature technical outcome"):
        ExperimentOutcomeMaturationInputV1.model_validate(payload)


def test_only_correction_events_may_supersede_an_outcome() -> None:
    payload = matured_input().model_dump(mode="python")
    payload["supersedes_event_id"] = "prior-event"
    with pytest.raises(ValueError, match="only OUTCOME_CORRECTED"):
        ExperimentOutcomeMaturationInputV1.model_validate(payload)


def test_every_source_artifact_requires_a_point_in_time_availability() -> None:
    payload = matured_input().model_dump(mode="python")
    payload["source_data_available_at"] = ()

    with pytest.raises(ValueError, match="every source artifact hash"):
        ExperimentOutcomeMaturationInputV1.model_validate(payload)


def test_learning_forward_action_without_candidate_artifact_is_not_trainable() -> None:
    registered = build_experiment_action(
        proposal=proposal_v2(),
        experiment_id="experiment-no-artifact",
        research_cycle_id="cycle-1",
        challenger_id="challenger-no-artifact",
        information_role=ExperimentInformationRole.LEARNING_FORWARD,
        decision_at=NOW,
        maturity_due_at=DUE,
        candidate_artifact_hash=None,
        evaluation_contract_hash=HASH_B,
        source_artifact_hashes=(),
        source_data_available_at=(),
        idempotency_key="action-no-artifact",
        created_at=NOW,
    )

    assert registered.meta_training_permitted is False
    event = build_outcome_event(
        action=registered,
        maturation=matured_input(role_suffix="no-artifact"),
        previous_event=None,
    )
    assert event.eligible_for_meta_training is False


def test_first_outcome_event_cannot_predate_its_registered_action() -> None:
    registered = action()
    backdated = ExperimentOutcomeMaturationInputV1(
        experiment_id=registered.experiment_id,
        event_kind=ExperimentOutcomeEventKind.TECHNICAL_OUTCOME_RECORDED,
        experiment_stage=ExperimentStage.BUILD,
        available_at=NOW - timedelta(seconds=1),
        maturity_status=ExperimentMaturityStatus.MATURED,
        technical_success=False,
        technical_failure_codes=("BUILD_FAILED",),
        evaluation_contract_hash=HASH_B,
        idempotency_key="backdated-first-event",
        created_at=NOW - timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="predate its registered action"):
        build_outcome_event(
            action=registered,
            maturation=backdated,
            previous_event=None,
        )


def test_learning_action_cannot_be_registered_after_maturity() -> None:
    registered = action()
    payload = registered.model_dump(
        mode="python",
        exclude={"action_hash"},
    )
    payload["created_at"] = DUE + timedelta(seconds=1)
    with pytest.raises(ValueError, match="registered before maturity"):
        ResearchExperimentActionV1.model_validate(
            {**payload, "action_hash": canonical_hash(payload)}
        )


def test_action_source_must_be_available_by_decision_time() -> None:
    registered = action()
    payload = registered.model_dump(
        mode="python",
        exclude={"action_hash"},
    )
    payload["source_data_available_at"] = (NOW + timedelta(seconds=1),)

    with pytest.raises(ValueError, match="unavailable at decision time"):
        ResearchExperimentActionV1.model_validate(
            {**payload, "action_hash": canonical_hash(payload)}
        )


def test_action_source_hashes_remain_paired_with_availability_times() -> None:
    earlier = NOW - timedelta(hours=1)
    registered = build_experiment_action(
        proposal=proposal_v2(),
        experiment_id="experiment-paired-action-sources",
        research_cycle_id="cycle-1",
        challenger_id="challenger-paired-action-sources",
        information_role=ExperimentInformationRole.LEARNING_FORWARD,
        decision_at=NOW,
        maturity_due_at=DUE,
        candidate_artifact_hash=HASH_A,
        evaluation_contract_hash=HASH_B,
        source_artifact_hashes=(HASH_A, HASH_B),
        source_data_available_at=(NOW, earlier),
        idempotency_key="action-paired-sources",
        created_at=NOW,
    )

    assert tuple(
        zip(
            registered.source_artifact_hashes,
            registered.source_data_available_at,
            strict=True,
        )
    ) == ((HASH_B, earlier), (HASH_A, NOW))


def test_outcome_source_hashes_remain_paired_with_availability_times() -> None:
    earlier = DUE - timedelta(hours=1)
    payload = matured_input().model_dump(mode="python")
    payload["source_artifact_hashes"] = (HASH_B, HASH_A)
    payload["source_data_available_at"] = (earlier, DUE)
    event = build_outcome_event(
        action=action(),
        maturation=ExperimentOutcomeMaturationInputV1.model_validate(payload),
        previous_event=None,
    )

    assert tuple(
        zip(
            event.source_artifact_hashes,
            event.source_data_available_at,
            strict=True,
        )
    ) == ((HASH_B, earlier), (HASH_A, DUE))


def test_outcome_availability_cannot_predate_registered_action() -> None:
    registered = action()
    maturation = ExperimentOutcomeMaturationInputV1(
        experiment_id=registered.experiment_id,
        event_kind=ExperimentOutcomeEventKind.TECHNICAL_OUTCOME_RECORDED,
        experiment_stage=ExperimentStage.BUILD,
        available_at=NOW - timedelta(seconds=1),
        maturity_status=ExperimentMaturityStatus.MATURED,
        technical_success=False,
        technical_failure_codes=("BUILD_FAILED",),
        evaluation_contract_hash=HASH_B,
        idempotency_key="backdated-availability",
        created_at=NOW,
    )

    with pytest.raises(ValueError, match="availability cannot predate"):
        build_outcome_event(
            action=registered,
            maturation=maturation,
            previous_event=None,
        )


def test_outcome_availability_cannot_regress_within_event_chain() -> None:
    registered = action()
    first_input = ExperimentOutcomeMaturationInputV1(
        experiment_id=registered.experiment_id,
        event_kind=ExperimentOutcomeEventKind.TECHNICAL_OUTCOME_RECORDED,
        experiment_stage=ExperimentStage.BUILD,
        available_at=NOW + timedelta(hours=1),
        maturity_status=ExperimentMaturityStatus.MATURED,
        technical_success=True,
        evaluation_contract_hash=HASH_B,
        idempotency_key="availability-first",
        created_at=NOW + timedelta(hours=1),
    )
    first = build_outcome_event(
        action=registered,
        maturation=first_input,
        previous_event=None,
    )
    regressed = first_input.model_copy(
        update={
            "available_at": NOW,
            "created_at": NOW + timedelta(hours=2),
            "idempotency_key": "availability-regressed",
        }
    )

    with pytest.raises(ValueError, match="availability time cannot regress"):
        build_outcome_event(
            action=registered,
            maturation=regressed,
            previous_event=first,
        )


def test_economic_window_cannot_predate_action_registration() -> None:
    registered = action()
    payload = matured_input().model_dump(mode="python")
    payload["evaluation_window_start"] = NOW - timedelta(seconds=1)
    maturation = ExperimentOutcomeMaturationInputV1.model_validate(payload)

    with pytest.raises(ValueError, match="predates experiment registration"):
        build_outcome_event(
            action=registered,
            maturation=maturation,
            previous_event=None,
        )
