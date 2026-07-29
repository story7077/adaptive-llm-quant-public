from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import model_payload
from trading.domain.hashing import canonical_data, canonical_hash, stable_id
from trading.persistence.models import (
    ChallengerManifestRow,
    MarketBarRow,
    MarketCalendarSessionRow,
    ResearchCandidateArtifactRow,
    ResearchCandidateProspectiveExecutionRow,
    ResearchCandidateProspectiveOutcomeRow,
    ResearchCandidateProspectiveRequestRow,
    ResearchReplayArtifactRow,
)
from trading.persistence.prospective_evaluation import (
    ProspectiveEvaluationPersistenceError,
    ProspectiveEvaluationRepository,
)
from trading.research.candidate_abi import (
    CandidateDecisionConstraintsV1,
    CandidateDecisionRequestV1,
    CandidateDecisionResponseV1,
    CandidateEvaluationVariantV1,
    CandidateFeatureValueV1,
    CandidateInstrumentInputV1,
    CandidateTargetV1,
    build_candidate_decision_request,
    build_candidate_decision_response,
)
from trading.research.candidate_evaluation import (
    CandidateEvaluationCohortEntryV2,
    CandidateEvaluationDatasetV2,
    CandidateEvaluationScenarioSourceBindingV2,
    CandidateEvaluationScenarioV1,
    build_candidate_evaluation_cohort_entry_v2,
    build_candidate_evaluation_cohort_manifest_v2,
    build_candidate_evaluation_dataset_v2,
    build_candidate_evaluation_scenario,
    build_candidate_evaluation_source_binding_v2,
    build_candidate_evaluation_source_manifest_v2,
    evaluate_candidate_twice,
)
from trading.research.config import load_research_config
from trading.research.evaluation_contracts import KnownFactorReturnV1
from trading.research.prospective import (
    ProspectiveExecutionEvidenceV1,
    ProspectiveExecutionStatus,
    ProspectiveRequestEvidenceV1,
    ProspectiveSourceBarV1,
    ProspectiveSourceManifestV1,
)
from trading.research.prospective_evaluation import (
    build_candidate_outcomes,
    load_prospective_evaluation_config,
)
from trading.research.prospective_outcomes import (
    ProspectiveOutcomeEvidenceV1,
    ProspectiveOutcomeSourceBarV1,
    build_prospective_outcome_evidence,
    load_prospective_outcome_config,
)
from trading.runtime.prospective_evaluation import (
    ProspectiveEvaluationService,
)

CHALLENGER_ID = "challenger-evaluation-persistence"
ARTIFACT_BUNDLE_ID = "candidate-bundle-evaluation-persistence"
ARTIFACT_HASH = "a" * 64
CONFIG_HASH = "b" * 64
CODE_HASH = "c" * 64
TEST_HASH = "d" * 64
SESSION_COUNT = 126


class _Executor:
    def execute(
        self,
        request: CandidateDecisionRequestV1,
    ) -> CandidateDecisionResponseV1:
        return build_candidate_decision_response(
            request=request,
            targets=(
                CandidateTargetV1(
                    symbol="QQQ",
                    score=1.0,
                    target_weight=0.5,
                ),
            ),
            diagnostics={"review_due": False},
        )


def _business_dates(first: date, count: int) -> tuple[date, ...]:
    sessions: list[date] = []
    cursor = first
    while len(sessions) < count:
        if cursor.weekday() < 5:
            sessions.append(cursor)
        cursor += timedelta(days=1)
    return tuple(sessions)


def _request(
    *,
    ordinal: int,
    session_date: date,
    prior_request_id: str | None,
    prior_execution_hash: str | None,
) -> ProspectiveRequestEvidenceV1:
    decision_time = datetime(
        session_date.year,
        session_date.month,
        session_date.day,
        15,
        tzinfo=UTC,
    )
    source_bar = ProspectiveSourceBarV1(
        bar_id=f"evaluation-source-{ordinal:03d}",
        symbol="QQQ",
        session_date=session_date - timedelta(days=1),
        source_event_time=decision_time - timedelta(days=1, hours=1),
        available_at=decision_time - timedelta(days=1),
        payload_hash=canonical_hash(("source", ordinal)),
    )
    source_payload = {
        "schema_version": "candidate_prospective_source_manifest_v1",
        "producer_version": "evaluation-persistence-v1",
        "challenger_id": CHALLENGER_ID,
        "candidate_artifact_hash": ARTIFACT_HASH,
        "parent_run_id": "synthetic-parent-run",
        "parent_portfolio_decision_id": (
            f"synthetic-parent-decision-{ordinal:03d}"
        ),
        "parent_decision_hash": canonical_hash(("decision", ordinal)),
        "parent_input_manifest_hash": canonical_hash(("input", ordinal)),
        "parent_scheduled_at": decision_time - timedelta(seconds=5),
        "evaluation_anchor_id": "synthetic-evaluation-anchor",
        "evaluation_anchor_hash": "e" * 64,
        "prior_prospective_request_id": prior_request_id,
        "prior_execution_hash": prior_execution_hash,
        "state_source": (
            "CASH_ONLY_AT_EVALUATION_ANCHOR"
            if prior_request_id is None
            else "PRIOR_VERIFIED_TARGETS"
        ),
        "market_dataset_version": "alpaca_iex_adjusted_all_v1",
        "signal_data_cutoff": decision_time,
        "completed_session_dates": (source_bar.session_date,),
        "source_bars": (source_bar,),
        "formula_contract_hash": "f" * 64,
        "host_config_manifest_hash": "1" * 64,
    }
    source = ProspectiveSourceManifestV1.model_validate(
        {
            **source_payload,
            "manifest_hash": canonical_hash(source_payload),
        }
    )
    feature_time = source_bar.available_at
    features = tuple(
        CandidateFeatureValueV1(
            name=name,
            value=value,
            source_event_time=source_bar.source_event_time,
            available_at=feature_time,
            source_revision=0,
            revision_available_at=feature_time,
            revision_was_known_at_cutoff=True,
            source_hash=canonical_hash((ordinal, name)),
        )
        for name, value in (
            ("parent_target_weight", 0.25),
            ("signal", 1.0),
        )
    )
    request = build_candidate_decision_request(
        request_id=f"evaluation-request-{ordinal:03d}",
        challenger_id=CHALLENGER_ID,
        candidate_artifact_hash=ARTIFACT_HASH,
        strategy_id="Q1-DET",
        strategy_version="2.0.0",
        decision_time=decision_time,
        signal_data_cutoff=decision_time,
        variant=CandidateEvaluationVariantV1(),
        instruments=(
            CandidateInstrumentInputV1(
                symbol="QQQ",
                current_weight=0.0 if ordinal == 0 else 0.5,
                membership_available_at=decision_time - timedelta(days=365),
                membership_valid_from=decision_time - timedelta(days=365),
                membership_valid_until=None,
                instrument_is_non_survivor=False,
                features=features,
            ),
        ),
        constraints=CandidateDecisionConstraintsV1(
            maximum_gross_weight=1.0,
            minimum_cash_weight=0.0,
            maximum_weight_by_symbol={"QQQ": 0.8},
            numeric_tolerance=1e-12,
        ),
        strategy_parameters={"signal_scale": 1.0},
        source_data_manifest_hash=source.manifest_hash,
    )
    payload = {
        "schema_version": "candidate_prospective_request_evidence_v1",
        "prospective_request_id": request.request_id,
        "challenger_id": CHALLENGER_ID,
        "candidate_artifact_bundle_id": ARTIFACT_BUNDLE_ID,
        "candidate_artifact_hash": ARTIFACT_HASH,
        "candidate_config_hash": CONFIG_HASH,
        "strategy_config_content_sha256": "2" * 64,
        "parent_run_id": "synthetic-parent-run",
        "parent_portfolio_decision_id": (
            f"synthetic-parent-decision-{ordinal:03d}"
        ),
        "parent_scheduled_at": decision_time - timedelta(seconds=5),
        "calendar_session_id": f"evaluation-calendar-{ordinal:03d}",
        "evaluation_anchor_id": "synthetic-evaluation-anchor",
        "prior_prospective_request_id": prior_request_id,
        "source_manifest": source,
        "request": request,
        "created_at": decision_time,
        "real_order_routing": False,
        "automatic_promotion_enabled": False,
        "challenger_lifecycle_advance_enabled": False,
        "shadow_activation_enabled": False,
    }
    return ProspectiveRequestEvidenceV1.model_validate(
        {**payload, "evidence_hash": canonical_hash(payload)}
    )


def _execution(
    request: ProspectiveRequestEvidenceV1,
) -> ProspectiveExecutionEvidenceV1:
    response = _Executor().execute(request.request)
    payload = {
        "schema_version": (
            "candidate_prospective_execution_evidence_v1"
        ),
        "execution_id": (
            f"evaluation-execution-{request.prospective_request_id[-3:]}"
        ),
        "prospective_request_id": request.prospective_request_id,
        "challenger_id": CHALLENGER_ID,
        "candidate_artifact_hash": ARTIFACT_HASH,
        "request_hash": request.request.request_hash,
        "status": ProspectiveExecutionStatus.SUCCEEDED,
        "runtime_attestation_hash": "3" * 64,
        "security_contract_hash": "4" * 64,
        "primary_response": response,
        "replay_response": response,
        "deterministic_match": True,
        "error_code": None,
        "created_at": request.request.decision_time,
        "real_order_routing": False,
        "evidence_recorded": True,
        "challenger_status_advanced": False,
        "shadow_started": False,
    }
    return ProspectiveExecutionEvidenceV1.model_validate(
        {**payload, "execution_hash": canonical_hash(payload)}
    )


def _outcome(
    *,
    request: ProspectiveRequestEvidenceV1,
    execution: ProspectiveExecutionEvidenceV1,
    config_dir: Path,
) -> ProspectiveOutcomeEvidenceV1:
    decision_date = request.request.decision_time.date()
    implementation_date, evaluation_date = _business_dates(
        decision_date + timedelta(days=1),
        2,
    )
    implementation_close = datetime(
        implementation_date.year,
        implementation_date.month,
        implementation_date.day,
        20,
        tzinfo=UTC,
    )
    evaluation_close = datetime(
        evaluation_date.year,
        evaluation_date.month,
        evaluation_date.day,
        20,
        tzinfo=UTC,
    )
    symbols = ("HYG", "IWM", "QQQ", "SOXX", "SPY", "TLT")
    source_bars = tuple(
        ProspectiveOutcomeSourceBarV1(
            bar_id=(
                f"evaluation-outcome-{request.prospective_request_id[-3:]}-"
                f"{session_index}-{symbol.lower()}"
            ),
            symbol=symbol,
            session_date=session_date,
            source_event_time=event_time,
            available_at=event_time + timedelta(minutes=5),
            adjusted_close=100.0 if session_index == 0 else 101.0,
            volume=1_000_000.0,
            payload_hash=canonical_hash(
                (
                    request.prospective_request_id,
                    session_index,
                    symbol,
                )
            ),
        )
        for session_index, (session_date, event_time) in enumerate(
            (
                (implementation_date, implementation_close),
                (evaluation_date, evaluation_close),
            )
        )
        for symbol in symbols
    )
    return build_prospective_outcome_evidence(
        request=request,
        execution=execution,
        config=load_prospective_outcome_config(config_dir),
        implementation_calendar_session_id=(
            f"evaluation-implementation-{request.prospective_request_id[-3:]}"
        ),
        evaluation_calendar_session_id=(
            f"evaluation-forward-{request.prospective_request_id[-3:]}"
        ),
        implementation_close_at=implementation_close,
        evaluation_close_at=evaluation_close,
        outcome_data_cutoff=evaluation_close + timedelta(hours=2),
        evaluation_nav_usd=100_000.0,
        candidate_current_weights={
            "QQQ": request.request.instruments[0].current_weight
        },
        candidate_target_weights={"QQQ": 0.5},
        baseline_current_weights={
            "QQQ": (
                0.0
                if request.prior_prospective_request_id is None
                else 0.25
            )
        },
        baseline_target_weights={"QQQ": 0.25},
        forward_returns={"QQQ": 0.01},
        adv_usd={"QQQ": 100_000_000.0},
        market_return=0.01,
        sector_return=0.01,
        known_factor_returns=(
            KnownFactorReturnV1(
                factor_id="CREDIT_HYG_MINUS_TLT",
                return_value=0.0,
            ),
            KnownFactorReturnV1(
                factor_id="SIZE_IWM_MINUS_QQQ",
                return_value=0.0,
            ),
        ),
        regime="UP",
        source_bars=source_bars,
        created_at=evaluation_close + timedelta(hours=2),
    )


def _dataset(
    *,
    requests: tuple[ProspectiveRequestEvidenceV1, ...],
    outcomes: tuple[ProspectiveOutcomeEvidenceV1, ...],
    config_dir: Path,
    tamper_first_outcome: bool = False,
) -> CandidateEvaluationDatasetV2:
    scenarios: list[CandidateEvaluationScenarioV1] = []
    bindings: list[CandidateEvaluationScenarioSourceBindingV2] = []
    entries: list[CandidateEvaluationCohortEntryV2] = []
    calendar_path: list[str] = []
    for ordinal, (request, outcome) in enumerate(
        zip(requests, outcomes, strict=True)
    ):
        scenario_outcomes = build_candidate_outcomes(outcome)
        if tamper_first_outcome and ordinal == 0:
            scenario_outcomes = (
                scenario_outcomes[0].model_copy(
                    update={"forward_return": 0.5}
                ),
            )
        transformation_hash = canonical_hash(
            ("evaluation-transform", ordinal, tamper_first_outcome)
        )
        scenario_id = stable_id(
            "candidate-evaluation-persistence-scenario",
            request.prospective_request_id,
            transformation_hash,
        )
        scenario = build_candidate_evaluation_scenario(
            scenario_id=scenario_id,
            request=request.request,
            outcomes=scenario_outcomes,
            evaluation_nav_usd=outcome.evaluation_nav_usd,
        )
        calendar_path.append(request.calendar_session_id)
        calendar_path_hash = canonical_hash(calendar_path)
        scenarios.append(scenario)
        bindings.append(
            build_candidate_evaluation_source_binding_v2(
                scenario=scenario,
                base_scenario_id=scenario_id,
                base_request_hash=request.request.request_hash,
                base_source_manifest_hash=(
                    request.source_manifest.manifest_hash
                ),
                calendar_path_hash=calendar_path_hash,
                outcome_source_hash=outcome.outcome_hash,
                transformation_hash=transformation_hash,
            )
        )
        entries.append(
            build_candidate_evaluation_cohort_entry_v2(
                prospective_request_id=request.prospective_request_id,
                request_hash=request.request.request_hash,
                decision_time=request.request.decision_time,
                signal_data_cutoff=request.request.signal_data_cutoff,
                outcome_source_hash=outcome.outcome_hash,
                outcome_available_at=outcome.outcome_available_at,
            )
        )
    cohort = build_candidate_evaluation_cohort_manifest_v2(
        selection_policy="FIRST_N_SUCCESSFUL_FORWARD_SESSIONS",
        required_successful_sessions=SESSION_COUNT,
        entries=tuple(entries),
        terminal_failure_hashes=(),
        terminal_request_count=SESSION_COUNT,
        selection_data_cutoff=max(
            item.outcome_data_cutoff for item in outcomes
        ),
    )
    evaluation_config = load_prospective_evaluation_config(config_dir)
    source_manifest = build_candidate_evaluation_source_manifest_v2(
        producer_version="evaluation-persistence-v2",
        config_manifest_hash=evaluation_config.manifest_hash,
        cohort_manifest=cohort,
        bindings=tuple(bindings),
    )
    return build_candidate_evaluation_dataset_v2(
        dataset_id=stable_id(
            "candidate-evaluation-persistence-dataset",
            source_manifest.manifest_hash,
        ),
        challenger_id=CHALLENGER_ID,
        candidate_artifact_hash=ARTIFACT_HASH,
        source_manifest=source_manifest,
        eligible_instrument_count=1,
        eligible_non_survivor_count=0,
        scenarios=tuple(scenarios),
    )


def _seed_evidence_rows(
    *,
    engine: Engine,
    requests: tuple[ProspectiveRequestEvidenceV1, ...],
    executions: tuple[ProspectiveExecutionEvidenceV1, ...],
    outcomes: tuple[ProspectiveOutcomeEvidenceV1, ...],
) -> None:
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        with Session(bind=connection, expire_on_commit=False) as session:
            session.add(
                ChallengerManifestRow(
                    challenger_id=CHALLENGER_ID,
                    proposal_id="synthetic-proposal",
                    strategy_id="Q1-DET",
                    strategy_version="2.0.0",
                    parent_version="1.0.0",
                    experiment_family="evaluation-persistence",
                    source_commit="5" * 40,
                    patch_hash="6" * 64,
                    code_hash=CODE_HASH,
                    config_hash=CONFIG_HASH,
                    test_manifest_hash=TEST_HASH,
                    initial_status="PROPOSED",
                    manifest_hash="7" * 64,
                    payload_json={},
                    created_at=requests[0].request.decision_time,
                )
            )
            session.add(
                ResearchCandidateArtifactRow(
                    bundle_id=ARTIFACT_BUNDLE_ID,
                    challenger_id=CHALLENGER_ID,
                    proposal_id="synthetic-proposal",
                    research_cycle_id="synthetic-research-cycle",
                    candidate_tree_hash="8" * 64,
                    code_hash=CODE_HASH,
                    config_hash=CONFIG_HASH,
                    test_manifest_hash=TEST_HASH,
                    declared_entrypoint="candidate.strategy:decide",
                    bundle_hash=ARTIFACT_HASH,
                    real_order_routing=False,
                    payload_json={},
                    created_at=requests[0].request.decision_time,
                )
            )
            for request, execution, outcome in zip(
                requests,
                executions,
                outcomes,
                strict=True,
            ):
                decision_time = request.request.decision_time
                session.add(
                    MarketCalendarSessionRow(
                        calendar_session_id=request.calendar_session_id,
                        algorithm_version="q1_math_core_v1",
                        calendar_version=outcome.calendar_version,
                        session_date=decision_time.date(),
                        open_at=decision_time - timedelta(hours=1, minutes=30),
                        close_at=decision_time + timedelta(hours=5),
                        source="TEST",
                        available_at=decision_time - timedelta(days=1),
                        config_manifest_hash="9" * 64,
                        code_version="test-code",
                        model_version="test-model",
                        source_manifest_hash=canonical_hash(
                            ("calendar-source", request.calendar_session_id)
                        ),
                        session_hash=canonical_hash(
                            ("calendar-session", request.calendar_session_id)
                        ),
                        payload_json={},
                        created_at=decision_time - timedelta(days=1),
                    )
                )
                session.add(
                    ResearchCandidateProspectiveRequestRow(
                        prospective_request_id=(
                            request.prospective_request_id
                        ),
                        challenger_id=CHALLENGER_ID,
                        candidate_artifact_bundle_id=ARTIFACT_BUNDLE_ID,
                        candidate_artifact_hash=ARTIFACT_HASH,
                        candidate_config_hash=CONFIG_HASH,
                        strategy_config_content_sha256=(
                            request.strategy_config_content_sha256
                        ),
                        parent_run_id=request.parent_run_id,
                        parent_portfolio_decision_id=(
                            request.parent_portfolio_decision_id
                        ),
                        calendar_session_id=request.calendar_session_id,
                        evaluation_anchor_id=request.evaluation_anchor_id,
                        prior_prospective_request_id=(
                            request.prior_prospective_request_id
                        ),
                        parent_scheduled_at=request.parent_scheduled_at,
                        signal_data_cutoff=(
                            request.request.signal_data_cutoff
                        ),
                        request_hash=request.request.request_hash,
                        source_manifest_hash=(
                            request.source_manifest.manifest_hash
                        ),
                        host_config_manifest_hash=(
                            request.source_manifest
                            .host_config_manifest_hash
                        ),
                        evidence_hash=request.evidence_hash,
                        real_order_routing=False,
                        payload_json=model_payload(request),
                        source_manifest_json=model_payload(
                            request.source_manifest
                        ),
                        created_at=request.created_at,
                    )
                )
                session.add(
                    ResearchCandidateProspectiveExecutionRow(
                        execution_id=execution.execution_id,
                        prospective_request_id=(
                            execution.prospective_request_id
                        ),
                        challenger_id=CHALLENGER_ID,
                        candidate_artifact_hash=ARTIFACT_HASH,
                        request_hash=execution.request_hash,
                        status=execution.status,
                        runtime_attestation_hash=(
                            execution.runtime_attestation_hash
                        ),
                        security_contract_hash=(
                            execution.security_contract_hash
                        ),
                        primary_response_hash=(
                            execution.primary_response.output_hash
                            if execution.primary_response is not None
                            else None
                        ),
                        replay_response_hash=(
                            execution.replay_response.output_hash
                            if execution.replay_response is not None
                            else None
                        ),
                        deterministic_match=True,
                        error_code=None,
                        success_identity=(
                            execution.prospective_request_id
                        ),
                        execution_hash=execution.execution_hash,
                        real_order_routing=False,
                        payload_json=model_payload(execution),
                        created_at=execution.created_at,
                    )
                )
                session.add(
                    ResearchCandidateProspectiveOutcomeRow(
                        outcome_id=outcome.outcome_id,
                        prospective_request_id=(
                            outcome.prospective_request_id
                        ),
                        execution_id=outcome.execution_id,
                        challenger_id=CHALLENGER_ID,
                        candidate_artifact_hash=ARTIFACT_HASH,
                        request_hash=outcome.request_hash,
                        execution_hash=outcome.execution_hash,
                        decision_calendar_session_id=(
                            outcome.decision_calendar_session_id
                        ),
                        implementation_calendar_session_id=(
                            outcome.implementation_calendar_session_id
                        ),
                        evaluation_calendar_session_id=(
                            outcome.evaluation_calendar_session_id
                        ),
                        calendar_version=outcome.calendar_version,
                        market_dataset_version=(
                            outcome.market_dataset_version
                        ),
                        decision_time=outcome.decision_time,
                        outcome_data_cutoff=outcome.outcome_data_cutoff,
                        outcome_available_at=outcome.outcome_available_at,
                        config_manifest_hash=(
                            outcome.config_manifest_hash
                        ),
                        source_manifest_hash=(
                            outcome.source_manifest_hash
                        ),
                        cost_model_hash=outcome.cost_model_hash,
                        outcome_hash=outcome.outcome_hash,
                        real_order_routing=False,
                        payload_json=model_payload(outcome),
                        created_at=outcome.created_at,
                    )
                )
            session.commit()
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def test_evaluation_dataset_and_trace_are_source_bound_and_idempotent(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    repository_root: Path,
) -> None:
    _, engine, factory = sqlite_database
    config_dir = repository_root / "config"
    session_dates = _business_dates(date(2026, 1, 2), SESSION_COUNT)
    requests: list[ProspectiveRequestEvidenceV1] = []
    executions: list[ProspectiveExecutionEvidenceV1] = []
    outcomes: list[ProspectiveOutcomeEvidenceV1] = []
    prior_request_id = None
    prior_execution_hash = None
    for ordinal, session_date in enumerate(session_dates):
        request = _request(
            ordinal=ordinal,
            session_date=session_date,
            prior_request_id=prior_request_id,
            prior_execution_hash=prior_execution_hash,
        )
        execution = _execution(request)
        outcome = _outcome(
            request=request,
            execution=execution,
            config_dir=config_dir,
        )
        requests.append(request)
        executions.append(execution)
        outcomes.append(outcome)
        prior_request_id = request.prospective_request_id
        prior_execution_hash = execution.execution_hash
    request_tuple = tuple(requests)
    execution_tuple = tuple(executions)
    outcome_tuple = tuple(outcomes)
    _seed_evidence_rows(
        engine=engine,
        requests=request_tuple,
        executions=execution_tuple,
        outcomes=outcome_tuple,
    )
    repository = ProspectiveEvaluationRepository(factory)
    evaluation_config = load_prospective_evaluation_config(config_dir)

    tampered = _dataset(
        requests=request_tuple,
        outcomes=outcome_tuple,
        config_dir=config_dir,
        tamper_first_outcome=True,
    )
    with pytest.raises(
        ProspectiveEvaluationPersistenceError,
        match="outcome payload is not source-bound",
    ):
        repository.store_dataset(
            dataset=tampered,
            config_manifest_hash=evaluation_config.manifest_hash,
            created_at=(
                tampered.source_manifest.cohort_manifest
                .selection_data_cutoff
            ),
        )

    dataset = _dataset(
        requests=request_tuple,
        outcomes=outcome_tuple,
        config_dir=config_dir,
    )
    created_at = (
        dataset.source_manifest.cohort_manifest.selection_data_cutoff
    )
    assert repository.store_dataset(
        dataset=dataset,
        config_manifest_hash=evaluation_config.manifest_hash,
        created_at=created_at,
    )
    assert not repository.store_dataset(
        dataset=dataset,
        config_manifest_hash=evaluation_config.manifest_hash,
        created_at=created_at,
    )
    assert repository.dataset(challenger_id=CHALLENGER_ID) == dataset

    evaluation_contract = (
        load_research_config(config_dir)
        .config.falsification.evaluation_contract
        .model_copy(
            update={
                "minimum_observation_count": SESSION_COUNT,
                "minimum_session_count": SESSION_COUNT,
            }
        )
    )
    evaluated = evaluate_candidate_twice(
        dataset=dataset,
        executor=_Executor(),
        replay_executor=_Executor(),
        evaluation_contract=evaluation_contract,
        trace_id="evaluation-trace-persistence",
        config_hash=CONFIG_HASH,
        code_hash=CODE_HASH,
        created_at=created_at,
    )
    replay = evaluated.replay
    with factory.begin() as session:
        session.add(
            ResearchReplayArtifactRow(
                replay_artifact_id="evaluation-replay-persistence",
                challenger_id=CHALLENGER_ID,
                candidate_artifact_hash=ARTIFACT_HASH,
                config_hash=CONFIG_HASH,
                code_hash=CODE_HASH,
                data_manifest_hash=(
                    dataset.source_manifest.manifest_hash
                ),
                first_replay_hash=replay.first_replay_hash,
                second_replay_hash=replay.second_replay_hash,
                deterministic_match=True,
                artifact_hash=replay.artifact_hash,
                payload_json=cast(
                    dict[str, Any],
                    canonical_data(replay.payload()),
                ),
                created_at=replay.created_at,
            )
        )
    assert repository.store_trace(
        dataset=dataset,
        trace=evaluated.trace,
        replay_artifact_hash=replay.artifact_hash,
    )
    assert not repository.store_trace(
        dataset=dataset,
        trace=evaluated.trace,
        replay_artifact_hash=replay.artifact_hash,
    )
    assert repository.trace(challenger_id=CHALLENGER_ID) == evaluated.trace
    status = repository.status(challenger_id=CHALLENGER_ID)
    assert status["status"] == "EVALUATION_TRACE_RECORDED"
    assert status["dataset"]["base_session_count"] == SESSION_COUNT
    assert status["dataset"]["scenario_count"] == SESSION_COUNT
    assert status["trace"]["replay_artifact_hash"] == replay.artifact_hash
    assert status["oos_started"] is False
    assert status["shadow_started"] is False
    assert status["automatic_promotion_enabled"] is False
    assert status["real_order_routing"] is False


def test_source_records_ignore_calendar_rows_recorded_after_decision(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    repository_root: Path,
) -> None:
    _, engine, factory = sqlite_database
    request = _request(
        ordinal=0,
        session_date=date(2026, 1, 2),
        prior_request_id=None,
        prior_execution_hash=None,
    )
    execution = _execution(request)
    outcome = _outcome(
        request=request,
        execution=execution,
        config_dir=repository_root / "config",
    )
    _seed_evidence_rows(
        engine=engine,
        requests=(request,),
        executions=(execution,),
        outcomes=(outcome,),
    )
    source = request.source_manifest.source_bars[0]
    with factory.begin() as session:
        session.add(
            MarketBarRow(
                bar_id=source.bar_id,
                provider="alpaca",
                feed="iex",
                symbol=source.symbol,
                timeframe="1Day",
                event_time=source.source_event_time,
                provider_timestamp=source.source_event_time.isoformat(),
                available_at=source.available_at,
                ingested_at=source.available_at,
                source_kind="TEST",
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100"),
                volume=Decimal("1000000"),
                vwap=Decimal("100"),
                trade_count=1,
                request_id="evaluation-calendar-pit-test",
                payload_hash=source.payload_hash,
                raw_object_uri=None,
                payload_json={
                    "_adjustment": "all",
                    "_dataset_version": (
                        request.source_manifest.market_dataset_version
                    ),
                },
            )
        )
    repository = ProspectiveEvaluationRepository(factory)
    before = repository.source_records(
        challenger_id=CHALLENGER_ID,
        maximum_records=1,
    )
    assert len(before.records) == 1

    decision_time = request.request.decision_time
    with factory.begin() as session:
        session.add(
            MarketCalendarSessionRow(
                calendar_session_id="late-recorded-historical-session",
                algorithm_version="q1_math_core_v1",
                calendar_version=outcome.calendar_version,
                session_date=decision_time.date() - timedelta(days=1),
                open_at=decision_time - timedelta(days=1, hours=1),
                close_at=decision_time - timedelta(hours=18),
                source="TEST_LATE_BACKFILL",
                available_at=decision_time - timedelta(days=30),
                config_manifest_hash="9" * 64,
                code_version="test-code",
                model_version="test-model",
                source_manifest_hash=canonical_hash(
                    "late-recorded-calendar-source"
                ),
                session_hash=canonical_hash(
                    "late-recorded-calendar-session"
                ),
                payload_json={},
                created_at=decision_time + timedelta(days=1),
            )
        )

    after = repository.source_records(
        challenger_id=CHALLENGER_ID,
        maximum_records=1,
    )
    assert after == before


def test_evaluation_service_waits_without_starting_candidate_runtime(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    repository_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, engine, factory = sqlite_database
    config_dir = repository_root / "config"
    request = _request(
        ordinal=0,
        session_date=date(2026, 1, 2),
        prior_request_id=None,
        prior_execution_hash=None,
    )
    execution = _execution(request)
    outcome = _outcome(
        request=request,
        execution=execution,
        config_dir=config_dir,
    )
    _seed_evidence_rows(
        engine=engine,
        requests=(request,),
        executions=(execution,),
        outcomes=(outcome,),
    )

    candidate_runtime_calls = 0

    def forbidden_candidate_runtime(*args: object, **kwargs: object) -> None:
        del args, kwargs
        nonlocal candidate_runtime_calls
        candidate_runtime_calls += 1
        raise AssertionError(
            "Candidate runtime started before the forward cohort matured"
        )

    monkeypatch.setattr(
        "trading.runtime.prospective_evaluation.connect_candidate_runtime",
        forbidden_candidate_runtime,
    )
    result = ProspectiveEvaluationService(
        factory,
        evaluation_config=load_prospective_evaluation_config(config_dir),
        outcome_config=load_prospective_outcome_config(config_dir),
        research_config=load_research_config(config_dir),
    ).run(
        challenger_id=CHALLENGER_ID,
        commander_root=tmp_path / "commander",
        commander_run=tmp_path / "commander-run",
    )

    assert result.status == "WAITING_FOR_FORWARD_OUTCOMES"
    assert result.successful_forward_sessions == 1
    assert result.required_forward_sessions == SESSION_COUNT
    assert result.dataset is None
    assert result.trace is None
    assert result.replay is None
    assert result.falsification_report is None
    assert candidate_runtime_calls == 0
    status = ProspectiveEvaluationRepository(factory).status(
        challenger_id=CHALLENGER_ID
    )
    assert status["status"] == "WAITING_FOR_FORWARD_OUTCOMES"
    assert status["oos_started"] is False
    assert status["shadow_started"] is False
    assert status["automatic_promotion_enabled"] is False
    assert status["real_order_routing"] is False
