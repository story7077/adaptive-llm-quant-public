from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.domain.hashing import canonical_hash
from trading.research.experiment_outcomes import (
    PredictionCalibrationSummaryV1,
    ResearchActionKind,
    ResearchMemorySnapshotV1,
)
from trading.research.meta_controller import (
    MetaControllerObservationV1,
    MetaControllerParametersV1,
    MetaControllerReasonCode,
    MetaControllerTrainingViewV1,
    ResearchContextV1,
    build_research_action_plan,
    build_research_context,
)

NOW = datetime(2026, 7, 28, 1, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
CONFIG_HASH = "d" * 64


def _context(
    *,
    regime: str = "regime-trend",
    failure: str = "failure-none",
    exposure: str = "exposure-balanced",
) -> ResearchContextV1:
    return build_research_context(
        regime_cluster_id=regime,
        failure_cluster_id=failure,
        portfolio_exposure_cluster_id=exposure,
    )


def _observation(
    *,
    event_hash: str,
    action: ResearchActionKind,
    context: ResearchContextV1 | None,
    lcb: float | None,
    technical_success: bool | None = True,
    turnover_delta: float | None = 0.0,
    drawdown_delta: float | None = 0.0,
    cost_delta_bps: float | None = 0.0,
    complexity_delta: float = 0.0,
) -> MetaControllerObservationV1:
    payload = {
        "schema_version": "meta_controller_observation_v1",
        "event_hash": event_hash,
        "research_cycle_id": f"cycle-{event_hash[0]}",
        "primary_action_kind": action,
        "context": context,
        "available_at": NOW,
        "technical_success": technical_success,
        "portfolio_delta_sharpe_lcb": lcb,
        "turnover_delta": turnover_delta,
        "drawdown_delta": drawdown_delta,
        "cost_delta_bps": cost_delta_bps,
        "complexity_delta": complexity_delta,
    }
    return MetaControllerObservationV1.model_validate(
        {**payload, "observation_hash": canonical_hash(payload)}
    )


def _snapshot(event_hashes: tuple[str, ...]) -> ResearchMemorySnapshotV1:
    payload = {
        "schema_version": "research_memory_snapshot_v1",
        "snapshot_id": "snapshot-meta-controller",
        "as_of": NOW,
        "data_available_cutoff": NOW,
        "included_event_hashes": event_hashes,
        "excluded_future_event_count": 0,
        "excluded_unmatured_event_count": 0,
        "excluded_oos_event_count": 0,
        "excluded_meta_audit_event_count": 0,
        "excluded_invalid_event_count": 0,
        "action_statistics": (),
        "recent_failure_clusters": (),
        "regime_action_statistics": (),
        "nearest_historical_analogs": (),
        "prediction_calibration_summary": PredictionCalibrationSummaryV1(
            observation_count=0,
        ),
        "created_at": NOW,
    }
    return ResearchMemorySnapshotV1.model_validate(
        {**payload, "snapshot_hash": canonical_hash(payload)}
    )


def _view(
    snapshot: ResearchMemorySnapshotV1,
    observations: tuple[MetaControllerObservationV1, ...],
) -> MetaControllerTrainingViewV1:
    ordered = tuple(
        sorted(observations, key=lambda item: (item.available_at, item.event_hash))
    )
    payload = {
        "schema_version": "meta_controller_training_view_v1",
        "research_memory_snapshot_hash": snapshot.snapshot_hash,
        "data_available_cutoff": snapshot.data_available_cutoff,
        "observations": ordered,
    }
    return MetaControllerTrainingViewV1.model_validate(
        {**payload, "view_hash": canonical_hash(payload)}
    )


def _parameters(
    *,
    maximum_actions: int = 2,
) -> MetaControllerParametersV1:
    return MetaControllerParametersV1(
        policy_version="hierarchical-contextual-ucb-v1",
        maximum_actions_per_cycle=maximum_actions,
        prior_strength=4.0,
        exploration_coefficient=0.25,
        exploration_floor=0.01,
        technical_failure_weight=0.25,
        reward_clip=1.0,
        turnover_penalty_weight=0.05,
        turnover_scale=1.0,
        drawdown_penalty_weight=0.10,
        drawdown_scale=0.05,
        cost_penalty_weight=0.05,
        cost_scale_bps=10.0,
        complexity_penalty_weight=0.05,
        complexity_scale=1.0,
    )


def _plan(
    observations: tuple[MetaControllerObservationV1, ...],
    *,
    actions: tuple[ResearchActionKind, ...],
    context: ResearchContextV1 | None = None,
    maximum_actions: int = 2,
    budget: int = 2,
):
    snapshot = _snapshot(tuple(item.event_hash for item in observations))
    return build_research_action_plan(
        research_cycle_id="cycle-current",
        snapshot=snapshot,
        training_view=_view(snapshot, observations),
        context=context or _context(),
        parameters=_parameters(maximum_actions=maximum_actions),
        config_hash=CONFIG_HASH,
        available_action_kinds=actions,
        maximum_total_submissions=budget,
        idempotency_key="plan-current",
        generated_at=NOW + timedelta(seconds=1),
    )


def test_same_snapshot_and_config_produce_identical_action_plan() -> None:
    observations = (
        _observation(
            event_hash=HASH_A,
            action=ResearchActionKind.ADD_FEATURE,
            context=_context(),
            lcb=0.2,
        ),
    )
    first = _plan(
        observations,
        actions=(
            ResearchActionKind.ADD_FEATURE,
            ResearchActionKind.CHANGE_EXIT_RULE,
        ),
    )
    second = _plan(
        observations,
        actions=(
            ResearchActionKind.CHANGE_EXIT_RULE,
            ResearchActionKind.ADD_FEATURE,
        ),
    )

    assert first.plan_hash == second.plan_hash
    assert first.ranked_actions == second.ranked_actions


def test_positive_matured_reward_raises_action_ranking() -> None:
    plan = _plan(
        (
            _observation(
                event_hash=HASH_A,
                action=ResearchActionKind.ADD_FEATURE,
                context=_context(),
                lcb=0.4,
            ),
            _observation(
                event_hash=HASH_B,
                action=ResearchActionKind.CHANGE_EXIT_RULE,
                context=_context(),
                lcb=-0.4,
            ),
        ),
        actions=(
            ResearchActionKind.ADD_FEATURE,
            ResearchActionKind.CHANGE_EXIT_RULE,
        ),
    )

    assert plan.ranked_actions[0].action_kind is ResearchActionKind.ADD_FEATURE
    assert (
        MetaControllerReasonCode.POSITIVE_MATURED_REWARD
        in plan.ranked_actions[0].reason_codes
    )


def test_high_technical_failure_rate_receives_separate_penalty() -> None:
    plan = _plan(
        (
            _observation(
                event_hash=HASH_A,
                action=ResearchActionKind.ADD_FEATURE,
                context=_context(),
                lcb=None,
                technical_success=False,
            ),
            _observation(
                event_hash=HASH_B,
                action=ResearchActionKind.CHANGE_EXIT_RULE,
                context=_context(),
                lcb=None,
                technical_success=True,
            ),
        ),
        actions=(
            ResearchActionKind.ADD_FEATURE,
            ResearchActionKind.CHANGE_EXIT_RULE,
        ),
    )
    by_action = {item.action_kind: item for item in plan.ranked_actions}

    assert (
        by_action[ResearchActionKind.ADD_FEATURE].technical_failure_penalty
        > 0
    )
    assert (
        by_action[ResearchActionKind.CHANGE_EXIT_RULE]
        .technical_failure_penalty
        == 0
    )


def test_sparse_exact_bucket_backs_off_to_regime_failure_bucket() -> None:
    current = _context(exposure="exposure-defensive")
    historical = _context(exposure="exposure-growth")
    plan = _plan(
        (
            _observation(
                event_hash=HASH_A,
                action=ResearchActionKind.ADD_REGIME_GATE,
                context=historical,
                lcb=0.2,
            ),
        ),
        actions=(ResearchActionKind.ADD_REGIME_GATE,),
        context=current,
        maximum_actions=1,
        budget=1,
    )

    assert (
        MetaControllerReasonCode.BACKOFF_REGIME_FAILURE
        in plan.ranked_actions[0].reason_codes
    )
    assert plan.ranked_actions[0].matured_sample_count == 1


def test_untried_action_can_win_exploration_against_failure_heavy_action() -> None:
    plan = _plan(
        (
            _observation(
                event_hash=HASH_A,
                action=ResearchActionKind.ADD_FEATURE,
                context=_context(),
                lcb=None,
                technical_success=False,
            ),
        ),
        actions=(
            ResearchActionKind.ADD_FEATURE,
            ResearchActionKind.REQUEST_NEW_DATA,
        ),
        maximum_actions=1,
        budget=1,
    )

    assert (
        plan.ranked_actions[0].action_kind
        is ResearchActionKind.REQUEST_NEW_DATA
    )
    assert (
        MetaControllerReasonCode.UNTRIED_EXPLORATION
        in plan.ranked_actions[0].reason_codes
    )


def test_training_view_rejects_future_observation() -> None:
    snapshot = _snapshot((HASH_A,))
    original = _observation(
        event_hash=HASH_A,
        action=ResearchActionKind.ADD_FEATURE,
        context=_context(),
        lcb=0.1,
    )
    observation_payload = original.model_dump(
        mode="python",
        exclude={"observation_hash"},
    )
    observation_payload["available_at"] = NOW + timedelta(seconds=1)
    observation = MetaControllerObservationV1.model_validate(
        {
            **observation_payload,
            "observation_hash": canonical_hash(observation_payload),
        }
    )
    payload = {
        "schema_version": "meta_controller_training_view_v1",
        "research_memory_snapshot_hash": snapshot.snapshot_hash,
        "data_available_cutoff": NOW,
        "observations": (observation,),
    }

    with pytest.raises(ValueError, match="future data"):
        MetaControllerTrainingViewV1.model_validate(
            {**payload, "view_hash": canonical_hash(payload)}
        )


def test_plan_rejects_observation_outside_snapshot() -> None:
    snapshot = _snapshot(())
    observation = _observation(
        event_hash=HASH_C,
        action=ResearchActionKind.ADD_FEATURE,
        context=_context(),
        lcb=0.1,
    )

    with pytest.raises(ValueError, match="outside its memory snapshot"):
        build_research_action_plan(
            research_cycle_id="cycle-current",
            snapshot=snapshot,
            training_view=_view(snapshot, (observation,)),
            context=_context(),
            parameters=_parameters(),
            config_hash=CONFIG_HASH,
            available_action_kinds=(ResearchActionKind.ADD_FEATURE,),
            maximum_total_submissions=1,
            idempotency_key="outside-snapshot",
            generated_at=NOW + timedelta(seconds=1),
        )
