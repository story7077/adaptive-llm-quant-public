from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from trading.domain.hashing import canonical_hash
from trading.persistence.experiment_outcomes import (
    ExperimentOutcomeRepository,
)
from trading.persistence.meta_controller import (
    MetaControllerPersistenceError,
    MetaControllerRepository,
)
from trading.persistence.research import ResearchRepository
from trading.research.contracts import (
    AvailableDataCatalogV1,
    AvailableInstrumentV1,
    ChallengerManifestV1,
    ChallengerStatus,
    ResearchCommanderKind,
    ResearchDecisionKind,
)
from trading.research.experiment_outcomes import (
    AlgorithmProposalV2,
    ResearchActionKind,
)
from trading.research.file_runtime import (
    ResearchPlaneFileRuntime,
    atomic_write_json,
)
from trading.research.host import build_research_request_v2
from trading.research.meta_controller import (
    MetaControllerParametersV1,
    build_research_context,
)
from trading.research.v2_contracts import ResearchDecisionV2

NOW = datetime(2026, 7, 28, 15, 0, tzinfo=UTC)
HASH_A = "a" * 64


def _parameters() -> MetaControllerParametersV1:
    return MetaControllerParametersV1(
        policy_version="hierarchical-contextual-ucb-v1",
        maximum_actions_per_cycle=3,
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


def _catalog() -> AvailableDataCatalogV1:
    payload = {
        "schema_version": "available_data_catalog_v1",
        "catalog_id": "catalog-meta-controller",
        "as_of": NOW,
        "data_available_cutoff": NOW,
        "instruments": [
            AvailableInstrumentV1(
                symbol="QQQ",
                asset_class="US_ETF",
                first_available_at=NOW - timedelta(days=1000),
                point_in_time_membership_available=True,
                daily_history_sessions=800,
                intraday_history_sessions=200,
                execution_supported=True,
            )
        ],
        "dataset_versions": {"daily": "pit-v1"},
    }
    return AvailableDataCatalogV1(
        **payload,
        catalog_hash=canonical_hash(payload),
    )


def _snapshot_and_plan(factory):
    outcomes = ExperimentOutcomeRepository(factory)
    snapshot, persisted = outcomes.materialize_memory(
        as_of=NOW,
        data_available_cutoff=NOW,
        created_at=NOW,
        persist=True,
    )
    assert persisted is True
    controller = MetaControllerRepository(factory)
    plan, created = controller.build_plan(
        research_cycle_id="cycle-meta-v2",
        snapshot_id=snapshot.snapshot_id,
        context=build_research_context(
            regime_cluster_id="regime-neutral",
            failure_cluster_id="failure-none",
            portfolio_exposure_cluster_id="exposure-balanced",
        ),
        parameters=_parameters(),
        config_hash=HASH_A,
        available_action_kinds=(
            ResearchActionKind.ADD_FEATURE,
            ResearchActionKind.CHANGE_EXIT_RULE,
        ),
        maximum_total_submissions=2,
        idempotency_key="meta-plan-cycle-meta-v2",
        generated_at=NOW,
        persist=True,
    )
    assert created is True
    return snapshot, plan


def _proposal(
    action_kind: ResearchActionKind,
) -> AlgorithmProposalV2:
    payload = {
        "schema_version": "algorithm_proposal_v2",
        "proposal_id": "proposal-outside-plan",
        "hypothesis_id": "hypothesis-outside-plan",
        "hypothesis": "A bounded change may improve the portfolio.",
        "economic_mechanism": "The change may diversify signal errors.",
        "why_current_model_failed": "The parent omitted the mechanism.",
        "parent_strategy_id": "CHAMPION",
        "parent_strategy_version": "1.0.0",
        "proposed_strategy_id": "CHALLENGER",
        "proposed_strategy_version": "1.1.0",
        "target_horizon": "DAILY",
        "target_universe": ["QQQ"],
        "required_data": ["adjusted_daily_bars"],
        "feature_changes": ["bounded feature"],
        "signal_formula_changes": [],
        "entry_rule_changes": [],
        "exit_rule_changes": [],
        "position_sizing_changes": [],
        "regime_activation_changes": [],
        "calibration_changes": [],
        "expected_edge_source": "Diversifying information.",
        "expected_failure_modes": ["NO_EDGE"],
        "invalidation_conditions": ["No forward lower bound."],
        "placebo_tests": ["date_shift"],
        "stress_tests": ["cost_3x"],
        "minimum_economic_effect": {"delta_sharpe_lcb": 0.01},
        "estimated_capacity": {"usd": 100000},
        "estimated_turnover": {"annualized": 2.0},
        "estimated_cost_sensitivity": {"cost_3x": 0.0},
        "files_allowed_to_change": [
            "src/trading/strategies/challengers/**",
        ],
        "tests_required": ["tests/candidates/test_challenger.py"],
        "evidence_source_ids": ["evidence-1"],
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


def test_plan_persistence_is_idempotent_and_append_only(
    sqlite_database,
) -> None:
    _, engine, factory = sqlite_database
    snapshot, plan = _snapshot_and_plan(factory)
    repository = MetaControllerRepository(factory)

    assert repository.store_plan(plan) is False
    assert repository.get_plan(plan.action_plan_id) == plan
    assert repository.get_plan_for_cycle(plan.research_cycle_id) == plan
    assert repository.build_training_view(
        snapshot_id=snapshot.snapshot_id
    ).research_memory_snapshot_hash == snapshot.snapshot_hash

    conflicting = plan.model_copy(
        update={
            "maximum_total_submissions": 3,
            "plan_hash": "f" * 64,
        }
    )
    with pytest.raises(
        MetaControllerPersistenceError,
        match="idempotency conflict",
    ):
        repository.store_plan(conflicting)

    for statement in (
        "UPDATE research_action_plans SET policy_version='mutated'",
        "DELETE FROM research_action_plans",
    ):
        with (
            engine.connect() as connection,
            connection.begin(),
            pytest.raises(DBAPIError, match="append-only"),
        ):
            connection.execute(text(statement))


def test_v2_request_uses_only_persisted_memory_and_plan(
    sqlite_database,
    tmp_path,
) -> None:
    _, _, factory = sqlite_database
    snapshot, plan = _snapshot_and_plan(factory)
    research = ResearchRepository(factory)
    selection = research.select_commander(
        ResearchCommanderKind.CODEX_SOL_MAX,
        config_hash=HASH_A,
        effective_at=NOW,
        created_at=NOW,
        expected_version=0,
    )
    request = build_research_request_v2(
        outcome_repository=ExperimentOutcomeRepository(factory),
        meta_controller_repository=MetaControllerRepository(factory),
        snapshot_id=snapshot.snapshot_id,
        action_plan_id=plan.action_plan_id,
        request_id="request-meta-v2",
        research_cycle_id=plan.research_cycle_id,
        commander_selection=selection,
        created_at=NOW,
        as_of=NOW,
        data_available_cutoff=NOW,
        expires_at=NOW + timedelta(hours=2),
        source_snapshot_commit="b" * 40,
        champion_version="1.0.0",
        experiment_family="meta-family",
        champion_manifest={"strategy_id": "CHAMPION"},
        active_challenger_manifests=[],
        execution_cost_summary={},
        capacity_summary={},
        recent_market_evidence=[],
        recent_web_research=[],
        available_data_catalog=_catalog(),
        allowed_change_scope=[
            "src/trading/strategies/challengers/**",
        ],
        forbidden_change_scope=["src/trading/risk/**"],
        experiment_budget={
            "family_submission_limit": 5,
            "family_submissions_used": 1,
            "oos_budget_limit": 2,
            "oos_budget_used": 0,
        },
    )

    assert request.research_memory_snapshot.snapshot_hash == snapshot.snapshot_hash
    assert request.research_action_plan.plan_hash == plan.plan_hash
    assert request.strategy_performance_summary["snapshot_hash"] == (
        snapshot.snapshot_hash
    )
    assert request.regime_summary["context"] == plan.context.model_dump(
        mode="json"
    )
    assert research.create_cycle(request) is True
    assert research.create_cycle(request) is False

    payload = {
        "schema_version": "research_decision_v2",
        "request_id": request.request_id,
        "research_cycle_id": request.research_cycle_id,
        "selected_commander": request.selected_commander,
        "commander_selection_id": request.commander_selection_id,
        "commander_selection_version": request.commander_selection_version,
        "source_snapshot_commit": request.source_snapshot_commit,
        "champion_version": request.champion_version,
        "experiment_family": request.experiment_family,
        "context_manifest_hash": request.context_manifest_hash,
        "request_schema_version": request.schema_version,
        "request_expires_at": request.expires_at,
        "decision": ResearchDecisionKind.NO_RESEARCH_CHANGE,
        "rationale": "The immutable evidence does not justify a revision.",
        "proposal": None,
        "requested_evidence": [],
        "research_memory_snapshot_hash": snapshot.snapshot_hash,
        "research_action_plan_hash": plan.plan_hash,
        "created_at": NOW + timedelta(minutes=1),
    }
    decision = ResearchDecisionV2.model_validate(
        {**payload, "output_hash": canonical_hash(payload)}
    )
    assert (
        research.accept_decision_v2(
            decision,
            received_at=NOW + timedelta(minutes=1),
        )
        is None
    )

    outside_proposal = _proposal(ResearchActionKind.REMOVE_FEATURE)
    outside_payload = {
        **payload,
        "decision": ResearchDecisionKind.PROPOSE_FEATURE_REVISION,
        "rationale": "A removal is proposed.",
        "proposal": outside_proposal,
        "created_at": NOW + timedelta(minutes=2),
    }
    outside_decision = ResearchDecisionV2.model_validate(
        {
            **outside_payload,
            "output_hash": canonical_hash(outside_payload),
        }
    )
    with pytest.raises(ValueError, match="outside the action plan"):
        outside_decision.assert_bound_to_v2(
            request,
            received_at=NOW + timedelta(minutes=2),
            current_selection=selection,
        )

    proposal = _proposal(ResearchActionKind.ADD_FEATURE)
    proposal_payload = {
        **payload,
        "decision": ResearchDecisionKind.PROPOSE_FEATURE_REVISION,
        "rationale": "The bounded feature is ready for isolated falsification.",
        "proposal": proposal,
        "created_at": NOW + timedelta(minutes=3),
    }
    proposal_decision = ResearchDecisionV2.model_validate(
        {
            **proposal_payload,
            "output_hash": canonical_hash(proposal_payload),
        }
    )
    assert research.accept_decision_v2(
        proposal_decision,
        received_at=NOW + timedelta(minutes=3),
    ) == proposal.proposal_id
    manifest_payload = {
        "schema_version": "challenger_manifest_v1",
        "challenger_id": "challenger-v2-file-runtime",
        "strategy_id": proposal.proposed_strategy_id,
        "strategy_version": proposal.proposed_strategy_version,
        "parent_version": proposal.parent_strategy_version,
        "hypothesis_id": proposal.hypothesis_id,
        "experiment_family": request.experiment_family,
        "source_commit": request.source_snapshot_commit,
        "patch_hash": "b" * 64,
        "proposal_hash": proposal.proposal_hash,
        "code_hash": "c" * 64,
        "config_hash": "d" * 64,
        "test_manifest_hash": "e" * 64,
        "created_by_commander": request.selected_commander,
        "implemented_by_builder": "CODEX_SOL_MAX",
        "evidence_source_ids": proposal.evidence_source_ids,
        "required_data": proposal.required_data,
        "decision_horizon": proposal.target_horizon,
        "execution_universe": proposal.target_universe,
        "estimated_turnover": proposal.estimated_turnover,
        "estimated_capacity": proposal.estimated_capacity,
        "status": ChallengerStatus.PROPOSED,
        "created_at": NOW + timedelta(minutes=4),
    }
    manifest = ChallengerManifestV1.model_validate(
        {
            **manifest_payload,
            "manifest_hash": canonical_hash(manifest_payload),
        }
    )
    decision_file = tmp_path / "decision-v2.json"
    manifest_file = tmp_path / "challenger-v2.json"
    assert atomic_write_json(decision_file, proposal_decision)
    assert atomic_write_json(manifest_file, manifest)

    registered_manifest, registered_proposal, created = (
        ResearchPlaneFileRuntime(
            repository=research,
            bundle_root=tmp_path / "runs",
        ).register_challenger(
            decision_file=decision_file,
            manifest_file=manifest_file,
        )
    )

    assert created is True
    assert registered_manifest == manifest
    assert registered_proposal == proposal
