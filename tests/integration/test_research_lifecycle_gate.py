from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from trading.domain.hashing import canonical_hash, stable_id
from trading.persistence.models import (
    AlgorithmProposalRow,
    ChallengerManifestRow,
    DomainEventRow,
    FalsificationReportRow,
    OosLockboxResultRow,
    ResearchCandidateArtifactRow,
    ResearchChampionDesignationRow,
    ResearchCommanderSelectionRow,
    ResearchCycleRow,
    ResearchPromotionEvidenceRow,
    ResearchReplayArtifactRow,
    ResearchShadowArmRegistrationRow,
    TrustedPromotionEvaluationRow,
)
from trading.persistence.research import (
    ResearchPersistenceError,
    ResearchRepository,
)
from trading.research.candidate_artifact import (
    CandidateArtifactBundleV1,
    CandidateRequestBindingV1,
    CandidateRuntimeV1,
    build_candidate_artifact_bundle,
)
from trading.research.contracts import (
    AlgorithmProposalV1,
    ChallengerManifestV1,
    ChallengerStatus,
    CommanderSelectionV1,
    FalsificationStatus,
    PromotionDecisionV1,
    PromotionVerdict,
    ResearchCommanderKind,
    ResearchRequestV1,
)
from trading.research.falsification import (
    MANDATORY_FALSIFICATION_TESTS,
    ExperimentBudget,
    build_falsification_report,
    make_test_result,
)
from trading.research.lifecycle import (
    ResearchLifecycleError,
    ResearchLifecycleService,
)
from trading.research.oos_lockbox import (
    OosEvaluationRequest,
    OosLockboxService,
    OosProcessEvaluationConfig,
    PrivateOosObservation,
)
from trading.research.promotion import REQUIRED_PROMOTION_CRITERIA
from trading.research.promotion_evidence import (
    PromotionEvaluationContractV1,
    build_promotion_evidence,
    build_trusted_shadow_performance_summary,
    evaluate_trusted_promotion_evidence,
)
from trading.research.replay import DeterministicReplayArtifactV1
from trading.research.shadow import ShadowArmIdentity, ShadowExecutionContract
from trading.research.shadow_runtime import MatchedShadowPerformanceSummaryV1

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)


def _research_request() -> ResearchRequestV1:
    payload = {
        "schema_version": "research_request_v1",
        "request_id": "request-1",
        "research_cycle_id": "cycle-1",
        "selected_commander": "CODEX_SOL_MAX",
        "commander_selection_id": "selection-1",
        "commander_selection_version": 1,
        "created_at": NOW,
        "as_of": NOW,
        "data_available_cutoff": NOW,
        "expires_at": datetime(2026, 7, 27, 22, 0, tzinfo=UTC),
        "source_snapshot_commit": "1" * 40,
        "champion_version": "1.0.0",
        "experiment_family": "family-1",
        "champion_manifest": {
            "strategy_id": "T1",
            "strategy_version": "1.0.0",
        },
        "active_challenger_manifests": [],
        "strategy_performance_summary": {},
        "failure_case_clusters": [],
        "regime_summary": {},
        "execution_cost_summary": {},
        "capacity_summary": {},
        "recent_market_evidence": [],
        "recent_web_research": [],
        "available_data_catalog": {"catalog_id": "catalog-1"},
        "allowed_change_scope": ["src/trading/strategies/**"],
        "forbidden_change_scope": ["src/trading/broker/**"],
        "experiment_budget": {
            "maximum_submissions": 5,
            "maximum_oos_uses": 5,
        },
    }
    return ResearchRequestV1.model_validate(
        {
            **payload,
            "context_manifest_hash": canonical_hash(payload),
        }
    )


def _proposal() -> AlgorithmProposalV1:
    payload = {
        "schema_version": "algorithm_proposal_v1",
        "proposal_id": "proposal-1",
        "hypothesis_id": "hypothesis-1",
        "hypothesis": "A bounded point-in-time signal may improve matched returns.",
        "economic_mechanism": "Slow information diffusion.",
        "why_current_model_failed": "The parent omits the proposed signal.",
        "parent_strategy_id": "T1",
        "parent_strategy_version": "1.0.0",
        "proposed_strategy_id": "T1",
        "proposed_strategy_version": "1.1.0",
        "target_horizon": "1d",
        "target_universe": ["SPY", "QQQ"],
        "required_data": ["pit_daily_bars"],
        "feature_changes": ["add bounded signal"],
        "signal_formula_changes": ["rank signal"],
        "entry_rule_changes": [],
        "exit_rule_changes": [],
        "position_sizing_changes": [],
        "regime_activation_changes": [],
        "calibration_changes": [],
        "expected_edge_source": "information diffusion",
        "expected_failure_modes": ["crowding"],
        "invalidation_conditions": ["non-positive matched return"],
        "placebo_tests": ["symbol shuffle"],
        "stress_tests": ["3x costs"],
        "minimum_economic_effect": {"annualized_excess_return": 0.01},
        "estimated_capacity": {"usd": 1000000},
        "estimated_turnover": {"annualized": 1.2},
        "estimated_cost_sensitivity": {"bps": 10},
        "files_allowed_to_change": ["src/trading/strategies/challengers/"],
        "tests_required": ["future_data_leakage"],
        "evidence_source_ids": ["evidence-1"],
        "raw_confidence": 0.4,
    }
    return AlgorithmProposalV1.model_validate(
        {
            **payload,
            "proposal_hash": canonical_hash(payload),
        }
    )


def _manifest(
    proposal: AlgorithmProposalV1,
    *,
    challenger_id: str = "challenger-1",
) -> ChallengerManifestV1:
    payload = {
        "schema_version": "challenger_manifest_v1",
        "challenger_id": challenger_id,
        "strategy_id": "T1",
        "strategy_version": "1.1.0",
        "parent_version": "1.0.0",
        "hypothesis_id": "hypothesis-1",
        "experiment_family": "family-1",
        "source_commit": "1" * 40,
        "patch_hash": "2" * 64,
        "proposal_hash": proposal.proposal_hash,
        "code_hash": "3" * 64,
        "config_hash": "4" * 64,
        "test_manifest_hash": "5" * 64,
        "created_by_commander": "CODEX_SOL_MAX",
        "implemented_by_builder": "CODEX_CANDIDATE_BUILDER",
        "evidence_source_ids": ["evidence-1"],
        "required_data": ["pit_daily_bars"],
        "decision_horizon": "1d",
        "execution_universe": ["SPY", "QQQ"],
        "estimated_turnover": {"annualized": 1.2},
        "estimated_capacity": {"usd": 1000000},
        "status": ChallengerStatus.PROPOSED,
        "created_at": NOW,
    }
    return ChallengerManifestV1(
        **payload,
        manifest_hash=canonical_hash(payload),
    )


def _candidate_bundle(
    *,
    challenger_id: str = "challenger-1",
):
    request = _research_request()
    proposal = _proposal()
    manifest = _manifest(proposal, challenger_id=challenger_id)
    return build_candidate_artifact_bundle(
        bundle_id=f"{challenger_id}-bundle-v1",
        challenger_id=challenger_id,
        request_binding=CandidateRequestBindingV1(
            request_id=request.request_id,
            research_cycle_id=request.research_cycle_id,
            context_manifest_hash=request.context_manifest_hash,
            source_snapshot_commit=request.source_snapshot_commit,
            champion_version=request.champion_version,
            experiment_family=request.experiment_family,
            selected_commander=request.selected_commander,
            commander_selection_id=request.commander_selection_id,
            commander_selection_version=request.commander_selection_version,
        ),
        source_snapshot_hash="6" * 64,
        candidate_tree_hash="a" * 64,
        code_hash=manifest.code_hash,
        config_hash=manifest.config_hash,
        patch_hash=manifest.patch_hash,
        proposal_hash=proposal.proposal_hash,
        builder_result_hash="b" * 64,
        test_manifest_hash=manifest.test_manifest_hash,
        challenger_manifest_hash=manifest.manifest_hash,
        validation_request_hash="c" * 64,
        runtime=CandidateRuntimeV1(
            implementation="cpython",
            version="3.13.5",
            abi_tag="cp313-win_amd64",
            executable_sha256="d" * 64,
        ),
        declared_entrypoint=("trading.strategies.challengers.t1_v1_1:decide"),
    )


CANDIDATE_ARTIFACT_HASH = _candidate_bundle().bundle_hash


class _Evaluator:
    def __init__(self, observations: list[PrivateOosObservation]) -> None:
        self.observations = observations
        self.calls = 0

    def evaluate(
        self,
        _: OosEvaluationRequest,
    ) -> list[PrivateOosObservation]:
        self.calls += 1
        return self.observations


class _BudgetLedger:
    def reserve(
        self,
        *,
        experiment_family: str,
        submission_number: int,
    ) -> int:
        assert experiment_family == "family-1"
        assert submission_number == 1
        return 1


def _seed_challenger(
    factory,
    *,
    challenger_id: str = "challenger-1",
    register_artifact: bool = True,
):
    request = _research_request()
    proposal = _proposal()
    manifest = _manifest(proposal, challenger_id=challenger_id)
    selection = CommanderSelectionV1(
        selection_id="selection-1",
        version=1,
        selected_commander=ResearchCommanderKind.CODEX_SOL_MAX,
        effective_at=NOW,
        created_at=NOW,
        config_hash="a" * 64,
    )
    with factory.begin() as session:
        session.add(
            ResearchCommanderSelectionRow(
                selection_id="selection-1",
                version=1,
                selected_commander="CODEX_SOL_MAX",
                effective_at=NOW,
                config_hash="a" * 64,
                payload_json=selection.model_dump(mode="json"),
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ResearchCycleRow(
                research_cycle_id="cycle-1",
                request_id="request-1",
                selection_id="selection-1",
                selection_version=1,
                selected_commander="CODEX_SOL_MAX",
                source_snapshot_commit=request.source_snapshot_commit,
                champion_version="1.0.0",
                experiment_family="family-1",
                as_of=NOW,
                data_available_cutoff=NOW,
                expires_at=request.expires_at,
                context_manifest_hash=request.context_manifest_hash,
                request_hash=canonical_hash(request),
                payload_json=request.model_dump(mode="json"),
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            AlgorithmProposalRow(
                proposal_id="proposal-1",
                research_cycle_id="cycle-1",
                hypothesis_id="hypothesis-1",
                parent_strategy_id="T1",
                parent_strategy_version="1.0.0",
                proposed_strategy_id="T1",
                proposed_strategy_version="1.1.0",
                proposal_hash=proposal.proposal_hash,
                evidence_manifest_hash=canonical_hash(sorted(proposal.evidence_source_ids)),
                payload_json=proposal.model_dump(mode="json"),
                created_at=NOW,
            )
        )
    repository = ResearchRepository(factory)
    assert repository.register_challenger(manifest, proposal_id="proposal-1")
    if register_artifact:
        assert repository.register_candidate_artifact(
            _candidate_bundle(challenger_id=challenger_id),
            created_at=NOW,
        )
    return repository


def _registered_manifest(factory) -> ChallengerManifestV1:
    with factory() as session:
        row = session.get(ChallengerManifestRow, "challenger-1")
        assert row is not None
        return ChallengerManifestV1.model_validate(row.payload_json)


def _copy_manifest(
    manifest: ChallengerManifestV1,
    **updates: object,
) -> ChallengerManifestV1:
    payload = manifest.model_dump(mode="python", exclude={"manifest_hash"})
    payload.update(updates)
    return ChallengerManifestV1(
        **payload,
        manifest_hash=canonical_hash(payload),
    )


def _report(*, failed_test: str | None = None):
    results = [
        make_test_result(
            test_id=test_id,
            status=(
                FalsificationStatus.FAIL if test_id == failed_test else FalsificationStatus.PASS
            ),
            reason_code=("MANDATORY_FAILURE" if test_id == failed_test else "PASSED"),
            metrics={
                "candidate_artifact_hash": CANDIDATE_ARTIFACT_HASH,
                "evaluation_contract_hash": "8" * 64,
                "data_manifest_hash": "9" * 64,
                "replay_hash": _replay().artifact_hash,
                "deterministic_seed": 17,
                "checked": True,
            },
        )
        for test_id in MANDATORY_FALSIFICATION_TESTS
    ]
    return build_falsification_report(
        challenger_id="challenger-1",
        results=results,
        budget=ExperimentBudget(
            experiment_family="family-1",
            submission_count=0,
            maximum_submissions=5,
            oos_budget_used=0,
            maximum_oos_budget=5,
        ),
        created_at=NOW,
    )


def _shadow_pair(
    *,
    challenger_contract: ShadowExecutionContract | None = None,
) -> tuple[ShadowArmIdentity, ShadowArmIdentity]:
    contract = ShadowExecutionContract(
        market_input_manifest_hash="6" * 64,
        decision_schedule_version="schedule-v1",
        execution_scenario_version="paper-conservative-v1",
        cost_model_version="cost-v1",
        starting_capital_usd="100000.00",
        liquidity_policy_version="liquidity-v1",
    )
    return (
        ShadowArmIdentity("champion-shadow-1", "T1", "1.0.0", contract),
        ShadowArmIdentity(
            "challenger-shadow-1",
            "T1",
            "1.1.0",
            contract if challenger_contract is None else challenger_contract,
        ),
    )


def _request() -> OosEvaluationRequest:
    return OosEvaluationRequest(
        challenger_id="challenger-1",
        experiment_family="family-1",
        submission_number=1,
        candidate_artifact_hash=CANDIDATE_ARTIFACT_HASH,
        evaluation_contract_hash="8" * 64,
    )


def _write_private_oos_dataset(private_root: Path) -> str:
    observations = [
        {
            "session_key": f"private-{index:03d}",
            "available_at": (NOW - timedelta(days=200 - index)).isoformat(),
            "candidate_return": 0.0015,
            "matched_baseline_return": 0.0005,
            "candidate_turnover": 0.10,
            "matched_baseline_turnover": 0.05,
        }
        for index in range(126)
    ]
    payload = {
        "schema_version": "oos_private_dataset_v1",
        "dataset_id": "integration-lockbox-v1",
        "candidate_artifact_hash": CANDIDATE_ARTIFACT_HASH,
        "evaluation_contract_hash": "8" * 64,
        "source_data_manifest_hash": "8" * 64,
        "candidate_replay_hash": "9" * 64,
        "trusted_producer_version": "trusted_candidate_evaluation_v1",
        "observations": observations,
    }
    dataset_hash = canonical_hash(payload)
    private_root.mkdir(parents=True, exist_ok=True)
    (private_root / "integration-lockbox-v1.json").write_text(
        json.dumps({**payload, "dataset_hash": dataset_hash}),
        encoding="utf-8",
    )
    return dataset_hash


def _replay(*, matched: bool = True) -> DeterministicReplayArtifactV1:
    return DeterministicReplayArtifactV1(
        challenger_id="challenger-1",
        candidate_artifact_hash=CANDIDATE_ARTIFACT_HASH,
        config_hash="4" * 64,
        code_hash="3" * 64,
        data_manifest_hash="9" * 64,
        first_replay_hash="a" * 64,
        second_replay_hash=("a" if matched else "b") * 64,
        created_at=NOW,
    )


def _service(repository, observations):
    evaluator = _Evaluator(observations)
    return (
        ResearchLifecycleService(
            repository=repository,
            oos_lockbox=OosLockboxService(
                evaluator=evaluator,
                budget_ledger=_BudgetLedger(),
                minimum_common_sessions=2,
                minimum_mean_daily_difference=0.001,
            ),
        ),
        evaluator,
    )


def _advance_to_shadow_running(factory):
    repository = _seed_challenger(factory)
    assert repository.record_replay_artifact(_replay())
    assert repository.record_falsification_report(_report())
    service, _ = _service(
        repository,
        [
            PrivateOosObservation("private-1", 0.02, 0.01),
            PrivateOosObservation("private-2", 0.03, 0.01),
        ],
    )
    champion, challenger = _shadow_pair()
    service.evaluate_oos_and_register_shadow(
        _request(),
        champion_shadow=champion,
        challenger_shadow=challenger,
        evaluated_at=NOW,
        persisted_at=NOW,
    )
    service.start_shadow(
        challenger_id="challenger-1",
        idempotency_key="explicit-shadow-start-1",
        created_at=NOW,
    )
    return repository, service


def _trusted_promotion_contract() -> PromotionEvaluationContractV1:
    return PromotionEvaluationContractV1(
        contract_version="research-promotion-thresholds-v1",
        minimum_common_oos_sessions=126,
        minimum_forward_sessions=63,
        minimum_independent_trades=30,
        minimum_annualized_net_excess_return_after_cost=0.0,
        minimum_matched_annualized_difference=0.0,
        minimum_economic_effect=0.01,
        maximum_drawdown=0.20,
        maximum_tail_loss=0.05,
        maximum_annualized_turnover=12.0,
        minimum_capacity_usd=100_000.0,
        minimum_regime_pass_fraction=0.67,
        maximum_runtime_error_rate=0.01,
    )


def _advance_to_trusted_shadow_running(factory):
    repository = _seed_challenger(factory)
    assert repository.record_replay_artifact(_replay())
    assert repository.record_falsification_report(_report())
    observations = [
        PrivateOosObservation(
            f"private-{index:03d}",
            0.002,
            0.001,
        )
        for index in range(126)
    ]
    service, _ = _service(repository, observations)
    champion, challenger = _shadow_pair()
    service.evaluate_oos_and_register_shadow(
        _request(),
        champion_shadow=champion,
        challenger_shadow=challenger,
        evaluated_at=NOW,
        persisted_at=NOW,
    )
    service.start_shadow(
        challenger_id="challenger-1",
        idempotency_key="explicit-shadow-start-trusted-1",
        created_at=NOW,
    )

    with factory() as session:
        registrations = list(
            session.scalars(
                select(ResearchShadowArmRegistrationRow)
                .where(ResearchShadowArmRegistrationRow.challenger_id == "challenger-1")
                .order_by(ResearchShadowArmRegistrationRow.arm_role)
            )
        )
    assert len(registrations) == 2
    by_role = {row.arm_role: row for row in registrations}
    champion_registration = by_role["CHAMPION"]
    challenger_registration = by_role["CHALLENGER"]
    shadow_pair_id = champion_registration.shadow_pair_id
    run_id = "trusted-shadow-run-1"
    daily_hashes: list[str] = []
    with factory.begin() as session:
        for index in range(63):
            available_at = NOW + timedelta(days=index + 1)
            event_payload = {
                "run_id": run_id,
                "shadow_pair_id": shadow_pair_id,
                "session_index": index + 1,
                "matched_daily_return_difference": "0.0005",
            }
            event_hash = canonical_hash(event_payload)
            daily_hashes.append(event_hash)
            session.add(
                DomainEventRow(
                    event_id=stable_id(
                        "trusted-shadow-daily-event",
                        shadow_pair_id,
                        index + 1,
                    ),
                    aggregate_type="RESEARCH_MATCHED_SHADOW_CYCLE",
                    aggregate_id=run_id,
                    event_type="MATCHED_PAPER_CYCLE_COMMITTED",
                    event_version="v1",
                    occurred_at=available_at,
                    available_at=available_at,
                    payload_json=event_payload,
                    payload_hash=event_hash,
                    causation_id=None,
                    correlation_id=shadow_pair_id,
                    idempotency_key=stable_id(
                        "trusted-shadow-daily-idempotency",
                        shadow_pair_id,
                        index + 1,
                    ),
                    created_at=available_at,
                )
            )
    source_payload = {
        "schema_version": "matched_shadow_performance_summary_v1",
        "run_id": run_id,
        "shadow_pair_id": shadow_pair_id,
        "common_sessions": 63,
        "champion_cumulative_return": Decimal("0.030"),
        "challenger_cumulative_return": Decimal("0.065"),
        "mean_matched_daily_return_difference": Decimal("0.0005"),
        "champion_turnover_usd": Decimal("10000"),
        "challenger_turnover_usd": Decimal("12000"),
        "champion_commission_usd": Decimal("10"),
        "challenger_commission_usd": Decimal("12"),
        "champion_execution_cost_usd": Decimal("20"),
        "challenger_execution_cost_usd": Decimal("24"),
        "champion_sensitivity_5bp_cost_usd": Decimal("30"),
        "challenger_sensitivity_5bp_cost_usd": Decimal("36"),
        "champion_sensitivity_10bp_cost_usd": Decimal("40"),
        "challenger_sensitivity_10bp_cost_usd": Decimal("48"),
        "champion_average_exposures": {"QQQ": Decimal("0.50")},
        "challenger_average_exposures": {"QQQ": Decimal("0.45")},
        "replay_hash": "b" * 64,
        "profitability_claimed": False,
    }
    source_summary = MatchedShadowPerformanceSummaryV1.model_validate(
        {
            **source_payload,
            "summary_hash": canonical_hash(source_payload),
        }
    )
    cutoff = NOW + timedelta(days=63)
    summary = build_trusted_shadow_performance_summary(
        summary_id="trusted-shadow-summary-1",
        challenger_id="challenger-1",
        current_champion_version="1.0.0",
        candidate_version="1.1.0",
        candidate_artifact_hash=CANDIDATE_ARTIFACT_HASH,
        champion_registration_hash=canonical_hash(champion_registration.payload_json),
        challenger_registration_hash=canonical_hash(challenger_registration.payload_json),
        execution_contract_hash=(champion_registration.execution_contract_hash),
        source_summary=source_summary,
        daily_evidence_hashes=tuple(daily_hashes),
        independent_trades=45,
        annualized_net_excess_return_after_cost=0.04,
        matched_annualized_difference=0.03,
        economic_effect=0.02,
        maximum_drawdown=0.10,
        tail_loss=0.03,
        annualized_turnover=2.0,
        estimated_capacity_usd=2_000_000.0,
        regime_pass_fraction=0.80,
        runtime_error_rate=0.001,
        data_available_cutoff=cutoff,
        created_at=cutoff,
    )
    return (
        repository,
        service,
        summary,
        _trusted_promotion_contract(),
        cutoff,
    )


def _promotion_decision(
    *,
    decision_id: str,
    verdict: PromotionVerdict,
    failed_criterion: str | None = None,
    replay_hash: str = "a" * 64,
    approved_by: str | None = None,
    criteria_override: dict[str, bool] | None = None,
) -> PromotionDecisionV1:
    criteria = (
        {name: True for name in REQUIRED_PROMOTION_CRITERIA}
        if criteria_override is None
        else criteria_override
    )
    if failed_criterion is not None:
        criteria[failed_criterion] = False
    failed = [
        name.upper()
        for name in REQUIRED_PROMOTION_CRITERIA
        if name in criteria and not criteria[name]
    ]
    payload = {
        "schema_version": "promotion_decision_v1",
        "promotion_decision_id": decision_id,
        "challenger_id": "challenger-1",
        "current_champion_version": "1.0.0",
        "candidate_version": "1.1.0",
        "verdict": verdict,
        "criteria": criteria,
        "failed_reason_codes": failed,
        "replay_hash": replay_hash,
        "automatic_promotion_enabled": False,
        "approved_by": approved_by,
        "created_at": NOW,
    }
    return PromotionDecisionV1(
        **payload,
        decision_hash=canonical_hash(payload),
    )


def test_exact_proposal_getter_and_challenger_retry_are_idempotent(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = _seed_challenger(factory)
    manifest = _registered_manifest(factory)

    proposal = repository.get_proposal("proposal-1")

    assert proposal is not None
    assert proposal.proposal_id == "proposal-1"
    assert proposal.proposal_hash == manifest.proposal_hash
    assert repository.get_proposal("proposal-missing") is None
    assert not repository.register_challenger(
        manifest,
        proposal_id="proposal-1",
    )


def test_challenger_registration_rejects_manifest_proposal_mismatch_atomically(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = _seed_challenger(factory)
    original = _registered_manifest(factory)
    mismatched = _copy_manifest(
        original,
        challenger_id="challenger-binding-mismatch",
        estimated_turnover={"annualized": 99.0},
    )

    with pytest.raises(
        ResearchPersistenceError,
        match="estimated_turnover",
    ):
        repository.register_challenger(
            mismatched,
            proposal_id="proposal-1",
        )

    with factory() as session:
        assert (
            session.get(
                ChallengerManifestRow,
                "challenger-binding-mismatch",
            )
            is None
        )


def test_challenger_strategy_version_is_unique_and_unknown_proposal_fails(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = _seed_challenger(factory)
    original = _registered_manifest(factory)
    duplicate_version = _copy_manifest(
        original,
        challenger_id="challenger-duplicate-version",
    )
    unknown_proposal = _copy_manifest(
        original,
        challenger_id="challenger-unknown-proposal",
    )

    with pytest.raises(
        ResearchPersistenceError,
        match="strategy version is already registered",
    ):
        repository.register_challenger(
            duplicate_version,
            proposal_id="proposal-1",
        )
    with pytest.raises(
        ResearchPersistenceError,
        match="unknown accepted proposal",
    ):
        repository.register_challenger(
            unknown_proposal,
            proposal_id="proposal-missing",
        )

    with factory() as session:
        registered = list(session.scalars(select(ChallengerManifestRow)))
    assert [row.challenger_id for row in registered] == ["challenger-1"]


def test_candidate_artifact_is_exactly_bound_idempotent_and_append_only(
    sqlite_database,
) -> None:
    _, engine, factory = sqlite_database
    repository = _seed_challenger(factory, register_artifact=False)
    bundle = _candidate_bundle()
    invalid_payload = bundle.model_dump(
        mode="python",
        exclude={"bundle_hash"},
    )
    invalid_payload["request_binding"]["context_manifest_hash"] = "e" * 64
    invalid = CandidateArtifactBundleV1.model_validate(
        {
            **invalid_payload,
            "bundle_hash": canonical_hash(invalid_payload),
        }
    )

    with pytest.raises(
        ResearchPersistenceError,
        match="trusted-input binding mismatch",
    ):
        repository.register_candidate_artifact(invalid, created_at=NOW)

    assert repository.register_candidate_artifact(bundle, created_at=NOW)
    assert not repository.register_candidate_artifact(bundle, created_at=NOW)
    assert repository.candidate_artifact("challenger-1") == bundle
    with factory() as session:
        row = session.scalar(select(ResearchCandidateArtifactRow))
        assert row is not None
        assert row.bundle_hash == bundle.bundle_hash
        assert row.real_order_routing is False

    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(text("UPDATE research_candidate_artifacts SET real_order_routing=true"))


def test_mandatory_falsification_failure_is_atomic_and_idempotent(
    sqlite_database,
) -> None:
    _, engine, factory = sqlite_database
    repository = _seed_challenger(factory)
    service, _ = _service(repository, [])
    assert repository.record_replay_artifact(_replay())
    report = _report(failed_test="future_data_leakage")

    first = service.record_falsification(report)
    second = service.record_falsification(report)

    assert first.created
    assert not second.created
    assert first.status is ChallengerStatus.TEST_FAILED
    assert repository.challenger_status("challenger-1") is ChallengerStatus.TEST_FAILED
    with pytest.raises(
        ResearchPersistenceError,
        match="trusted Research Lifecycle gate",
    ):
        repository.transition_challenger(
            challenger_id="challenger-1",
            to_status=ChallengerStatus.TEST_FAILED,
            reason_code="BYPASS",
            artifact_hash=None,
            idempotency_key="bypass",
            created_at=NOW,
        )
    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text(
                "UPDATE falsification_reports SET mandatory_passed=true "
                "WHERE challenger_id='challenger-1'"
            )
        )


def test_oos_failure_requires_passed_report_and_rejects_challenger(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = _seed_challenger(factory)
    service, evaluator = _service(repository, [])
    champion, challenger = _shadow_pair()
    raw_result = service._oos_lockbox.evaluate(_request(), evaluated_at=NOW)

    with pytest.raises(
        ResearchPersistenceError,
        match="matching deterministic replay",
    ):
        repository.record_falsification_report(_report())
    with pytest.raises(
        ResearchPersistenceError,
        match="passed mandatory falsification",
    ):
        repository.store_oos_result(
            raw_result,
            created_at=NOW,
            candidate_artifact_hash=_request().candidate_artifact_hash,
        )
    assert repository.record_replay_artifact(_replay())
    assert repository.record_falsification_report(_report())
    evaluator.calls = 0

    result = service.evaluate_oos_and_register_shadow(
        _request(),
        champion_shadow=champion,
        challenger_shadow=challenger,
        evaluated_at=NOW,
        persisted_at=NOW,
    )

    assert result.created
    assert result.status is ChallengerStatus.OOS_REJECTED
    assert repository.shadow_pair("challenger-1") == ()
    assert evaluator.calls == 1


def test_oos_pass_registers_matched_independent_paper_arms_then_starts(
    sqlite_database,
) -> None:
    _, engine, factory = sqlite_database
    repository = _seed_challenger(factory)
    assert repository.record_replay_artifact(_replay())
    assert repository.record_falsification_report(_report())
    assert not repository.record_replay_artifact(_replay())
    service, evaluator = _service(
        repository,
        [
            PrivateOosObservation("private-1", 0.02, 0.01),
            PrivateOosObservation("private-2", 0.03, 0.01),
        ],
    )
    champion, challenger = _shadow_pair()

    pending = service.evaluate_oos_and_register_shadow(
        _request(),
        champion_shadow=champion,
        challenger_shadow=challenger,
        evaluated_at=NOW,
        persisted_at=NOW,
    )
    replay = service.evaluate_oos_and_register_shadow(
        _request(),
        champion_shadow=champion,
        challenger_shadow=challenger,
        evaluated_at=NOW,
        persisted_at=NOW,
    )

    assert pending.created
    assert pending.status is ChallengerStatus.SHADOW_PENDING
    assert not replay.created
    assert replay.result.result_hash == pending.result.result_hash
    assert evaluator.calls == 1
    registrations = repository.shadow_pair("challenger-1")
    assert len(registrations) == 2
    assert {row["arm_role"] for row in registrations} == {
        "CHAMPION",
        "CHALLENGER",
    }
    assert len({row["arm_id"] for row in registrations}) == 2
    assert len({row["execution_contract_hash"] for row in registrations}) == 1
    assert all(row["real_order_routing"] is False for row in registrations)

    started = service.start_shadow(
        challenger_id="challenger-1",
        idempotency_key="explicit-shadow-start-1",
        created_at=NOW,
    )
    restarted = service.start_shadow(
        challenger_id="challenger-1",
        idempotency_key="explicit-shadow-start-1",
        created_at=NOW,
    )
    assert started.created
    assert not restarted.created
    assert restarted.status is ChallengerStatus.SHADOW_RUNNING
    status = repository.status()
    assert len(status["falsification_reports"]) == 1
    assert len(status["replay_artifacts"]) == 1
    assert len(status["shadow_arm_registrations"]) == 2

    with factory() as session:
        rows = list(session.scalars(select(ResearchShadowArmRegistrationRow)))
    assert len(rows) == 2
    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text("UPDATE research_shadow_arm_registrations SET real_order_routing=true")
        )


def test_mismatched_shadow_contract_fails_before_oos_runs(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = _seed_challenger(factory)
    assert repository.record_replay_artifact(_replay())
    assert repository.record_falsification_report(_report())
    service, evaluator = _service(
        repository,
        [
            PrivateOosObservation("private-1", 0.02, 0.01),
            PrivateOosObservation("private-2", 0.03, 0.01),
        ],
    )
    mismatch = ShadowExecutionContract(
        market_input_manifest_hash="9" * 64,
        decision_schedule_version="schedule-v1",
        execution_scenario_version="paper-conservative-v1",
        cost_model_version="cost-v1",
        starting_capital_usd="100000.00",
        liquidity_policy_version="liquidity-v1",
    )
    champion, challenger = _shadow_pair(challenger_contract=mismatch)

    with pytest.raises(ResearchLifecycleError, match="share execution conditions"):
        service.evaluate_oos_and_register_shadow(
            _request(),
            champion_shadow=champion,
            challenger_shadow=challenger,
            evaluated_at=NOW,
            persisted_at=NOW,
        )

    assert evaluator.calls == 0
    assert repository.challenger_status("challenger-1") is ChallengerStatus.PROPOSED


def test_replay_mismatch_is_persisted_and_blocks_oos(
    sqlite_database,
) -> None:
    _, engine, factory = sqlite_database
    repository = _seed_challenger(factory)
    service, evaluator = _service(
        repository,
        [
            PrivateOosObservation("private-1", 0.02, 0.01),
            PrivateOosObservation("private-2", 0.03, 0.01),
        ],
    )

    outcome = service.record_deterministic_replay(_replay(matched=False))
    replay = service.record_deterministic_replay(_replay(matched=False))

    assert outcome.status is ChallengerStatus.REPLAY_FAILED
    assert not replay.created
    assert not repository.has_passed_replay(
        challenger_id="challenger-1",
        candidate_artifact_hash=_request().candidate_artifact_hash,
    )
    champion, challenger = _shadow_pair()
    with pytest.raises(
        ResearchLifecycleError,
        match="mandatory falsification",
    ):
        service.evaluate_oos_and_register_shadow(
            _request(),
            champion_shadow=champion,
            challenger_shadow=challenger,
            evaluated_at=NOW,
            persisted_at=NOW,
        )
    assert evaluator.calls == 0
    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(text("UPDATE research_replay_artifacts SET deterministic_match=true"))


def test_promotion_eligibility_is_atomic_idempotent_and_not_promotion(
    sqlite_database,
) -> None:
    _, engine, factory = sqlite_database
    repository, service = _advance_to_shadow_running(factory)
    decision = _promotion_decision(
        decision_id="promotion-eligibility-1",
        verdict=PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL,
    )

    first = service.record_promotion_eligibility(decision)
    second = service.record_promotion_eligibility(decision)

    assert first.created
    assert not second.created
    assert first.status is ChallengerStatus.PROMOTION_ELIGIBLE
    assert repository.challenger_status("challenger-1") is (ChallengerStatus.PROMOTION_ELIGIBLE)
    with pytest.raises(
        ResearchPersistenceError,
        match="trusted Research Lifecycle gate",
    ):
        repository.transition_challenger(
            challenger_id="challenger-1",
            to_status=ChallengerStatus.PROMOTED,
            reason_code="BYPASS",
            artifact_hash=None,
            idempotency_key="promote-bypass",
            created_at=NOW,
        )
    with pytest.raises(
        ResearchPersistenceError,
        match="trusted Research Lifecycle gate",
    ):
        repository.store_promotion_decision(decision)
    gate = repository.status()["promotion_gate"]
    assert gate == {
        "eligible_challenger_ids": [],
        "manually_approved_challenger_ids": [],
        "explicit_human_designation_available": False,
        "automatic_promotion_enabled": False,
        "champion_mutation_available": False,
        "real_order_routing": False,
    }
    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text("UPDATE research_promotion_decisions SET automatic_promotion_enabled=true")
        )


def test_ineligible_decision_is_persisted_without_status_transition(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository, service = _advance_to_shadow_running(factory)
    decision = _promotion_decision(
        decision_id="promotion-ineligible-1",
        verdict=PromotionVerdict.INELIGIBLE,
        failed_criterion="tail_risk",
    )

    result = service.record_promotion_eligibility(decision)

    assert result.created
    assert result.status is ChallengerStatus.SHADOW_RUNNING
    assert repository.status()["promotion_decisions"][0]["verdict"] == "INELIGIBLE"


def test_promotion_gate_rejects_missing_criteria_and_wrong_replay(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository, service = _advance_to_shadow_running(factory)
    missing = _promotion_decision(
        decision_id="promotion-missing-criteria",
        verdict=PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL,
        criteria_override={"minimum_independent_trades": True},
    )
    wrong_replay = _promotion_decision(
        decision_id="promotion-wrong-replay",
        verdict=PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL,
        replay_hash="c" * 64,
    )

    with pytest.raises(ResearchLifecycleError, match="criteria mismatch"):
        service.record_promotion_eligibility(missing)
    with pytest.raises(ResearchLifecycleError, match="replay hash"):
        service.record_promotion_eligibility(wrong_replay)

    assert repository.challenger_status("challenger-1") is (ChallengerStatus.SHADOW_RUNNING)
    assert repository.status()["promotion_decisions"] == []


def test_manual_approval_is_separate_and_never_mutates_champion(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository, service = _advance_to_shadow_running(factory)
    eligibility = _promotion_decision(
        decision_id="promotion-eligibility-1",
        verdict=PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL,
    )
    service.record_promotion_eligibility(eligibility)
    approval = _promotion_decision(
        decision_id="promotion-manual-approval-1",
        verdict=PromotionVerdict.MANUALLY_APPROVED,
        approved_by="manual-reviewer",
    )

    with pytest.raises(
        ResearchLifecycleError,
        match="explicit manual approval path",
    ):
        service.record_promotion_eligibility(approval)
    first = service.record_manual_promotion_approval(approval)
    second = service.record_manual_promotion_approval(approval)

    assert first.created
    assert not second.created
    assert first.status is ChallengerStatus.PROMOTION_ELIGIBLE
    assert repository.challenger_status("challenger-1") is (ChallengerStatus.PROMOTION_ELIGIBLE)
    status = repository.status()
    assert status["promotion_gate"]["manually_approved_challenger_ids"] == []
    assert status["promotion_gate"]["champion_mutation_available"] is False
    assert all(
        challenger["current_status"] != ChallengerStatus.PROMOTED.value
        for challenger in status["challengers"]
    )


def test_trusted_promotion_requires_immutable_evidence_and_manual_designation(
    sqlite_database,
) -> None:
    _, engine, factory = sqlite_database
    (
        repository,
        service,
        summary,
        contract,
        cutoff,
    ) = _advance_to_trusted_shadow_running(factory)

    first_summary = service.record_shadow_performance_summary(summary)
    repeated_summary = service.record_shadow_performance_summary(summary)
    assert first_summary.created
    assert not repeated_summary.created

    evaluated_at = cutoff + timedelta(minutes=1)
    first_evaluation = service.evaluate_trusted_promotion(
        challenger_id="challenger-1",
        contract=contract,
        created_at=evaluated_at,
    )
    repeated_evaluation = service.evaluate_trusted_promotion(
        challenger_id="challenger-1",
        contract=contract,
        created_at=evaluated_at + timedelta(minutes=5),
    )
    assert first_evaluation.created
    assert not repeated_evaluation.created
    assert (
        repeated_evaluation.evaluation.evaluation_hash
        == first_evaluation.evaluation.evaluation_hash
    )
    assert repeated_evaluation.evidence.evidence_hash == first_evaluation.evidence.evidence_hash
    assert first_evaluation.status is ChallengerStatus.PROMOTION_ELIGIBLE
    assert first_evaluation.evidence.shadow_summary_hash == summary.summary_hash
    assert first_evaluation.evidence.common_oos_sessions == 126
    assert first_evaluation.evidence.mandatory_tests_passed
    assert first_evaluation.evidence.replay_reproducible
    assert first_evaluation.evaluation.decision.automatic_promotion_enabled is False
    with factory() as session:
        falsification = session.scalar(select(FalsificationReportRow))
        oos = session.scalar(select(OosLockboxResultRow))
        replay = session.scalar(select(ResearchReplayArtifactRow))
    assert falsification is not None
    assert oos is not None
    assert replay is not None
    assert first_evaluation.evidence.falsification_report_hash == falsification.report_hash
    assert first_evaluation.evidence.oos_result_hash == oos.result_hash
    assert (
        first_evaluation.evidence.candidate_artifact_hash
        == oos.candidate_artifact_hash
        == replay.candidate_artifact_hash
    )
    assert (
        first_evaluation.evidence.replay_hash
        == replay.first_replay_hash
        == replay.second_replay_hash
    )

    with pytest.raises(
        ResearchLifecycleError,
        match="explicit manual approval",
    ):
        service.designate_champion(
            challenger_id="challenger-1",
            expected_current_version="1.0.0",
            designated_by="human-reviewer",
            idempotency_key="designation-before-approval",
            designated_at=evaluated_at + timedelta(minutes=1),
        )

    approval_at = evaluated_at + timedelta(minutes=2)
    approval = service.approve_trusted_promotion(
        challenger_id="challenger-1",
        approved_by="human-reviewer",
        created_at=approval_at,
    )
    repeated_approval = service.approve_trusted_promotion(
        challenger_id="challenger-1",
        approved_by="human-reviewer",
        created_at=approval_at,
    )
    assert approval.created
    assert not repeated_approval.created
    assert approval.status is ChallengerStatus.PROMOTION_ELIGIBLE
    assert repository.current_champion_designation() is None
    approval_gate = repository.status()["promotion_gate"]
    assert approval_gate["eligible_challenger_ids"] == ["challenger-1"]
    assert approval_gate["manually_approved_challenger_ids"] == ["challenger-1"]
    assert approval_gate["explicit_human_designation_available"] is True

    with pytest.raises(
        ResearchLifecycleError,
        match="expected-current-version conflict",
    ):
        service.designate_champion(
            challenger_id="challenger-1",
            expected_current_version="0.9.0",
            designated_by="human-reviewer",
            idempotency_key="designation-wrong-expected-version",
            designated_at=approval_at + timedelta(minutes=1),
        )

    designation_at = approval_at + timedelta(minutes=2)
    designated = service.designate_champion(
        challenger_id="challenger-1",
        expected_current_version="1.0.0",
        designated_by="human-reviewer",
        idempotency_key="designation-trusted-1",
        designated_at=designation_at,
    )
    repeated_designation = service.designate_champion(
        challenger_id="challenger-1",
        expected_current_version="1.0.0",
        designated_by="human-reviewer",
        idempotency_key="designation-trusted-1",
        designated_at=designation_at,
    )
    assert designated.created
    assert not repeated_designation.created
    assert designated.status is ChallengerStatus.PROMOTED
    assert designated.designation.strategy_version == "1.1.0"
    assert designated.designation.automatic_promotion_enabled is False
    assert designated.designation.real_order_routing is False

    status = repository.status()
    assert status["current_champion"]["strategy_version"] == "1.1.0"
    assert status["promotion_gate"] == {
        "eligible_challenger_ids": ["challenger-1"],
        "manually_approved_challenger_ids": ["challenger-1"],
        "explicit_human_designation_available": False,
        "automatic_promotion_enabled": False,
        "champion_mutation_available": False,
        "real_order_routing": False,
    }
    with factory() as session:
        assert len(list(session.scalars(select(ResearchChampionDesignationRow)))) == 1

    for table in (
        "research_shadow_performance_summaries",
        "research_promotion_evidence",
        "trusted_promotion_evaluations",
        "research_champion_designations",
    ):
        with (
            engine.connect() as connection,
            connection.begin(),
            pytest.raises(DBAPIError, match="append-only"),
        ):
            connection.execute(text(f"UPDATE {table} SET payload_json=payload_json"))


def test_forged_promotion_metric_cannot_enter_trusted_evaluation(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    (
        repository,
        service,
        summary,
        contract,
        cutoff,
    ) = _advance_to_trusted_shadow_running(factory)
    service.record_shadow_performance_summary(summary)
    evidence = repository.build_trusted_promotion_evidence(
        challenger_id="challenger-1",
        created_at=cutoff + timedelta(minutes=1),
    )
    tampered_payload = evidence.model_dump(
        mode="python",
        exclude={"evidence_hash", "schema_version"},
    )
    tampered_payload["economic_effect"] = evidence.economic_effect + 1.0
    tampered = build_promotion_evidence(**tampered_payload)
    evaluation = evaluate_trusted_promotion_evidence(
        evidence=tampered,
        contract=contract,
        created_at=cutoff + timedelta(minutes=1),
    )

    with pytest.raises(
        ResearchPersistenceError,
        match="promotion metrics do not match shadow summary",
    ):
        repository.record_trusted_promotion_evaluation(
            evidence=tampered,
            contract=contract,
            evaluation=evaluation,
        )

    assert repository.challenger_status("challenger-1") is (ChallengerStatus.SHADOW_RUNNING)
    with factory() as session:
        assert list(session.scalars(select(ResearchPromotionEvidenceRow))) == []
        assert list(session.scalars(select(TrustedPromotionEvaluationRow))) == []


def test_concurrent_champion_designation_commits_exactly_one(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    (
        repository,
        service,
        summary,
        contract,
        cutoff,
    ) = _advance_to_trusted_shadow_running(factory)
    service.record_shadow_performance_summary(summary)
    evaluated_at = cutoff + timedelta(minutes=1)
    service.evaluate_trusted_promotion(
        challenger_id="challenger-1",
        contract=contract,
        created_at=evaluated_at,
    )
    approval_at = evaluated_at + timedelta(minutes=1)
    service.approve_trusted_promotion(
        challenger_id="challenger-1",
        approved_by="human-reviewer",
        created_at=approval_at,
    )

    def designate(key: str) -> tuple[bool, str]:
        try:
            result = service.designate_champion(
                challenger_id="challenger-1",
                expected_current_version="1.0.0",
                designated_by="human-reviewer",
                idempotency_key=key,
                designated_at=approval_at + timedelta(minutes=1),
            )
        except ResearchLifecycleError as exc:
            return False, str(exc)
        return result.created, result.designation.designation_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                designate,
                ("concurrent-designation-a", "concurrent-designation-b"),
            )
        )

    assert sum(created for created, _ in outcomes) == 1
    assert repository.challenger_status("challenger-1") is (ChallengerStatus.PROMOTED)
    with factory() as session:
        designations = list(session.scalars(select(ResearchChampionDesignationRow)))
    assert len(designations) == 1
    assert repository.current_champion_designation() is not None


def test_production_oos_service_uses_persistent_budget_and_process_worker(
    sqlite_database,
    tmp_path: Path,
) -> None:
    _, _, factory = sqlite_database
    repository = _seed_challenger(factory)
    private_root = tmp_path / "private-oos"
    dataset_hash = _write_private_oos_dataset(private_root)
    service = OosLockboxService.production(
        repository=repository,
        private_root=private_root,
        config=OosProcessEvaluationConfig(
            dataset_id="integration-lockbox-v1",
            dataset_manifest_hash=dataset_hash,
            data_available_cutoff=NOW,
            expected_source_data_manifest_hash="8" * 64,
            expected_candidate_replay_hash="9" * 64,
            expected_trusted_producer_version=("trusted_candidate_evaluation_v1"),
            minimum_common_sessions=126,
            minimum_mean_daily_difference=0.0005,
            annualization_sessions=252,
            newey_west_lag=5,
            bootstrap_seed=7077,
            bootstrap_block_length=10,
            bootstrap_samples=100,
            base_cost_bps=10,
            request_ttl_seconds=900,
            worker_timeout_seconds=60,
            cost_sensitivity_bps=(0, 5, 10),
            maximum_submissions=1,
            maximum_oos_uses=1,
        ),
        clock=lambda: NOW,
    )

    first = service.evaluate(_request(), evaluated_at=NOW)
    replay = service.evaluate(
        _request(),
        evaluated_at=NOW + timedelta(minutes=1),
    )

    assert first == replay
    assert first.verdict.value == "PASS"
    assert first.common_sessions == 126
    assert repository.budget_totals("family-1") == {
        "submissions": 1,
        "oos_budget_used": 1,
        "hypotheses": 0,
        "failures": 0,
    }
    with factory() as session:
        assert (
            session.execute(text("SELECT COUNT(*) FROM oos_budget_reservations")).scalar_one() == 1
        )


def test_oos_budget_reservation_is_append_only_and_capacity_bounded(
    sqlite_database,
) -> None:
    _, engine, factory = sqlite_database
    repository = _seed_challenger(factory)
    request = _request()
    reservation = repository.reserve_oos_budget(
        request=request,
        maximum_submissions=1,
        maximum_oos_uses=1,
        idempotency_key="reserve-one",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    replay = repository.reserve_oos_budget(
        request=request,
        maximum_submissions=1,
        maximum_oos_uses=1,
        idempotency_key="reserve-one",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    assert replay == reservation

    with pytest.raises(ResearchPersistenceError, match="budget exhausted"):
        repository.reserve_oos_budget(
            request=replace(request, submission_number=2),
            maximum_submissions=1,
            maximum_oos_uses=1,
            idempotency_key="reserve-two",
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
        )

    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text("UPDATE oos_budget_reservations SET oos_budget_ordinal=oos_budget_ordinal")
        )


def test_concurrent_oos_reservations_cannot_overrun_family_budget(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = _seed_challenger(factory)

    def reserve(idempotency_key: str) -> str:
        try:
            repository.reserve_oos_budget(
                request=_request(),
                maximum_submissions=1,
                maximum_oos_uses=1,
                idempotency_key=idempotency_key,
                created_at=NOW,
                expires_at=NOW + timedelta(minutes=15),
            )
        except ResearchPersistenceError:
            return "REJECTED"
        return "RESERVED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, ("concurrent-a", "concurrent-b")))

    assert sorted(outcomes) == ["REJECTED", "RESERVED"]
    assert repository.budget_totals("family-1")["oos_budget_used"] == 1
