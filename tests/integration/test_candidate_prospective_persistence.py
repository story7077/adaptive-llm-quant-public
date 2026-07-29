from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import model_payload
from trading.domain.hashing import canonical_hash
from trading.persistence.models import (
    AlgorithmProposalRow,
    ChallengerManifestRow,
    MarketCalendarSessionRow,
    PaperCycleRow,
    PortfolioDecisionRow,
    ResearchCandidateArtifactRow,
    ResearchCommanderSelectionRow,
    ResearchCycleRow,
    RunRow,
    StrategyEvaluationAnchorRow,
)
from trading.persistence.prospective import ProspectiveCandidateRepository
from trading.research.candidate_abi import (
    CandidateDecisionConstraintsV1,
    CandidateEvaluationVariantV1,
    CandidateFeatureValueV1,
    CandidateInstrumentInputV1,
    CandidateTargetV1,
    build_candidate_decision_request,
    build_candidate_decision_response,
)
from trading.research.candidate_artifact import (
    CandidateRequestBindingV1,
    CandidateRuntimeV1,
    build_candidate_artifact_bundle,
)
from trading.research.candidate_process import (
    CandidateProcessLimitsV1,
    build_candidate_execution_security,
)
from trading.research.commander_candidate import CandidateRuntimeAttestationV1
from trading.research.contracts import ResearchCommanderKind
from trading.research.prospective import (
    ProspectiveRequestEvidenceV1,
    ProspectiveSourceBarV1,
    ProspectiveSourceManifestV1,
    build_successful_execution_evidence,
    load_prospective_candidate_config,
)
from trading.runtime.prospective_candidate import ProspectiveCandidateCollector

NOW = datetime(2026, 7, 29, 14, 0, 5, tzinfo=UTC)
CONFIG_FILES = [
    {
        "path": "config/strategies/challengers/q1-det-v2.0.0.yaml",
        "sha256": "9" * 64,
    }
]
CONFIG_HASH = canonical_hash(CONFIG_FILES)


def _artifact():
    return build_candidate_artifact_bundle(
        bundle_id="candidate-bundle-persistence",
        challenger_id="challenger-persistence",
        request_binding=CandidateRequestBindingV1(
            request_id="research-request-persistence",
            research_cycle_id="research-cycle-persistence",
            context_manifest_hash="a" * 64,
            source_snapshot_commit="1" * 40,
            champion_version="1.0.0",
            experiment_family="prospective-persistence",
            selected_commander=ResearchCommanderKind.CODEX_SOL_MAX,
            commander_selection_id="selection-persistence",
            commander_selection_version=1,
        ),
        source_snapshot_hash="b" * 64,
        candidate_tree_hash="c" * 64,
        code_hash="d" * 64,
        config_hash=CONFIG_HASH,
        patch_hash="f" * 64,
        proposal_hash="1" * 64,
        builder_result_hash="2" * 64,
        test_manifest_hash="3" * 64,
        challenger_manifest_hash="4" * 64,
        validation_request_hash="5" * 64,
        runtime=CandidateRuntimeV1(
            implementation="CPython",
            version="3.13.12",
            abi_tag="cpython-313",
            executable_sha256="6" * 64,
        ),
        declared_entrypoint="candidate.strategy:decide",
    )


def _request_evidence(artifact) -> ProspectiveRequestEvidenceV1:
    source_bar = ProspectiveSourceBarV1(
        bar_id="bar-qqq-prospective",
        symbol="QQQ",
        session_date=date(2026, 7, 28),
        source_event_time=NOW - timedelta(days=1),
        available_at=NOW - timedelta(days=1) + timedelta(minutes=1),
        payload_hash="7" * 64,
    )
    source_payload = {
        "schema_version": "candidate_prospective_source_manifest_v1",
        "producer_version": "producer-v1",
        "challenger_id": artifact.challenger_id,
        "candidate_artifact_hash": artifact.bundle_hash,
        "parent_run_id": "parent-run-persistence",
        "parent_portfolio_decision_id": "parent-decision-persistence",
        "parent_decision_hash": "8" * 64,
        "parent_input_manifest_hash": "9" * 64,
        "parent_scheduled_at": NOW - timedelta(seconds=5),
        "evaluation_anchor_id": "anchor-persistence",
        "evaluation_anchor_hash": "a" * 64,
        "prior_prospective_request_id": None,
        "prior_execution_hash": None,
        "state_source": "CASH_ONLY_AT_EVALUATION_ANCHOR",
        "market_dataset_version": "test-adjusted-v1",
        "signal_data_cutoff": NOW,
        "completed_session_dates": (date(2026, 7, 28),),
        "source_bars": (source_bar,),
        "formula_contract_hash": "b" * 64,
        "host_config_manifest_hash": "c" * 64,
    }
    source = ProspectiveSourceManifestV1.model_validate(
        {**source_payload, "manifest_hash": canonical_hash(source_payload)}
    )
    feature = CandidateFeatureValueV1(
        name="signal",
        value=1.0,
        source_event_time=source_bar.source_event_time,
        available_at=source_bar.available_at,
        source_revision=0,
        revision_available_at=source_bar.available_at,
        revision_was_known_at_cutoff=True,
        source_hash="d" * 64,
    )
    request = build_candidate_decision_request(
        request_id="candidate-prospective-persistence",
        challenger_id=artifact.challenger_id,
        candidate_artifact_hash=artifact.bundle_hash,
        strategy_id="Q1-DET",
        strategy_version="2.0.0",
        decision_time=NOW,
        signal_data_cutoff=NOW,
        variant=CandidateEvaluationVariantV1(),
        instruments=(
            CandidateInstrumentInputV1(
                symbol="QQQ",
                current_weight=0,
                membership_available_at=NOW - timedelta(days=365),
                membership_valid_from=NOW - timedelta(days=365),
                membership_valid_until=None,
                instrument_is_non_survivor=False,
                features=(feature,),
            ),
        ),
        constraints=CandidateDecisionConstraintsV1(
            maximum_gross_weight=1,
            minimum_cash_weight=0,
            maximum_weight_by_symbol={"QQQ": 0.8},
            numeric_tolerance=1e-12,
        ),
        strategy_parameters={"signal_scale": 1.0},
        source_data_manifest_hash=source.manifest_hash,
    )
    payload = {
        "schema_version": "candidate_prospective_request_evidence_v1",
        "prospective_request_id": request.request_id,
        "challenger_id": artifact.challenger_id,
        "candidate_artifact_bundle_id": artifact.bundle_id,
        "candidate_artifact_hash": artifact.bundle_hash,
        "candidate_config_hash": artifact.config_hash,
        "strategy_config_content_sha256": "e" * 64,
        "parent_run_id": "parent-run-persistence",
        "parent_portfolio_decision_id": "parent-decision-persistence",
        "parent_scheduled_at": NOW - timedelta(seconds=5),
        "calendar_session_id": "calendar-persistence",
        "evaluation_anchor_id": "anchor-persistence",
        "prior_prospective_request_id": None,
        "source_manifest": source,
        "request": request,
        "created_at": NOW,
        "real_order_routing": False,
        "automatic_promotion_enabled": False,
        "challenger_lifecycle_advance_enabled": False,
        "shadow_activation_enabled": False,
    }
    return ProspectiveRequestEvidenceV1.model_validate(
        {**payload, "evidence_hash": canonical_hash(payload)}
    )


def _seed_dependencies(
    factory: sessionmaker[Session],
    artifact,
) -> None:
    with factory.begin() as session:
        session.add(
            RunRow(
                run_id="parent-run-persistence",
                mode="PAPER",
                experiment_version="q1_math_core_v1",
                config_manifest_hash="a" * 64,
                code_commit="test-code",
                started_at=NOW - timedelta(hours=1),
                ended_at=None,
                status="RUNNING",
                result_manifest=None,
                result_hash=None,
            )
        )
        session.add(
            MarketCalendarSessionRow(
                calendar_session_id="calendar-persistence",
                algorithm_version="q1_math_core_v1",
                calendar_version="test-calendar-v1",
                session_date=date(2026, 7, 29),
                open_at=NOW - timedelta(minutes=30),
                close_at=NOW + timedelta(hours=6),
                source="TEST",
                available_at=NOW - timedelta(days=2),
                config_manifest_hash="a" * 64,
                code_version="test-code",
                model_version="test-model",
                source_manifest_hash="b" * 64,
                session_hash="c" * 64,
                payload_json={},
                created_at=NOW - timedelta(days=2),
            )
        )
        session.flush()
        session.add(
            PaperCycleRow(
                cycle_id="cycle-persistence",
                run_id="parent-run-persistence",
                cycle_kind="Q1_STRATEGIC",
                scheduled_at=NOW - timedelta(seconds=5),
                data_available_cutoff=NOW - timedelta(seconds=5),
                status="COMPLETED",
                idempotency_key="cycle-persistence",
                lease_owner=None,
                lease_expires_at=None,
                attempt_count=1,
                input_manifest_hash="9" * 64,
                output_manifest_hash="f" * 64,
                started_at=NOW - timedelta(seconds=5),
                completed_at=NOW,
                last_error_code=None,
                last_error_detail=None,
                created_at=NOW - timedelta(seconds=5),
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            PortfolioDecisionRow(
                portfolio_decision_id="parent-decision-persistence",
                run_id="parent-run-persistence",
                arm_id="Q1-DET",
                source_cycle_id="cycle-persistence",
                input_state_sequence=1,
                decision_time=NOW,
                algorithm_version="q1_math_core_v1",
                scheduled_at=NOW - timedelta(seconds=5),
                signal_data_cutoff=NOW - timedelta(seconds=5),
                portfolio_state_as_of=NOW - timedelta(seconds=1),
                quote_as_of=NOW - timedelta(seconds=1),
                decision_created_at=NOW,
                valid_until=NOW + timedelta(minutes=20),
                calendar_session_id="calendar-persistence",
                config_manifest_hash="a" * 64,
                code_version="test-code",
                model_version="test-model",
                source_manifest_hash="b" * 64,
                input_manifest_hash="9" * 64,
                payload_json={},
                decision_hash="8" * 64,
            )
        )
        session.add(
            StrategyEvaluationAnchorRow(
                evaluation_anchor_id="anchor-persistence",
                run_id="parent-run-persistence",
                algorithm_version="q1_math_core_v1",
                calendar_session_id="calendar-persistence",
                common_t0_at=NOW - timedelta(minutes=1),
                initial_nav_usd=Decimal("100000"),
                quote_manifest_hash="d" * 64,
                config_manifest_hash="a" * 64,
                code_version="test-code",
                model_version="test-model",
                source_manifest_hash="b" * 64,
                anchor_hash="a" * 64,
                payload_json={},
                created_at=NOW - timedelta(seconds=30),
            )
        )
        session.flush()
        session.add(
            ResearchCommanderSelectionRow(
                selection_id="selection-persistence",
                version=1,
                selected_commander="CODEX_SOL_MAX",
                effective_at=NOW - timedelta(days=1),
                config_hash="e" * 64,
                payload_json={},
                created_at=NOW - timedelta(days=1),
            )
        )
        session.flush()
        session.add(
            ResearchCycleRow(
                research_cycle_id="research-cycle-persistence",
                request_id="research-request-persistence",
                selection_id="selection-persistence",
                selection_version=1,
                selected_commander="CODEX_SOL_MAX",
                source_snapshot_commit="1" * 40,
                champion_version="1.0.0",
                experiment_family="prospective-persistence",
                as_of=NOW - timedelta(days=1),
                data_available_cutoff=NOW - timedelta(days=1),
                expires_at=NOW + timedelta(days=1),
                context_manifest_hash="a" * 64,
                request_hash="b" * 64,
                payload_json={},
                created_at=NOW - timedelta(days=1),
            )
        )
        session.flush()
        session.add(
            AlgorithmProposalRow(
                proposal_id="proposal-persistence",
                research_cycle_id="research-cycle-persistence",
                hypothesis_id="hypothesis-persistence",
                parent_strategy_id="Q1-DET",
                parent_strategy_version="1.0.0",
                proposed_strategy_id="Q1-DET",
                proposed_strategy_version="2.0.0",
                proposal_hash=artifact.proposal_hash,
                evidence_manifest_hash="c" * 64,
                payload_json={},
                created_at=NOW - timedelta(days=1),
            )
        )
        session.flush()
        session.add(
            ChallengerManifestRow(
                challenger_id=artifact.challenger_id,
                proposal_id="proposal-persistence",
                strategy_id="Q1-DET",
                strategy_version="2.0.0",
                parent_version="1.0.0",
                experiment_family="prospective-persistence",
                source_commit="1" * 40,
                patch_hash=artifact.patch_hash,
                code_hash=artifact.code_hash,
                config_hash=artifact.config_hash,
                test_manifest_hash=artifact.test_manifest_hash,
                initial_status="PROPOSED",
                manifest_hash=artifact.challenger_manifest_hash,
                payload_json={},
                created_at=NOW - timedelta(days=1),
            )
        )
        session.flush()
        session.add(
            ResearchCandidateArtifactRow(
                bundle_id=artifact.bundle_id,
                challenger_id=artifact.challenger_id,
                proposal_id="proposal-persistence",
                research_cycle_id="research-cycle-persistence",
                candidate_tree_hash=artifact.candidate_tree_hash,
                code_hash=artifact.code_hash,
                config_hash=artifact.config_hash,
                test_manifest_hash=artifact.test_manifest_hash,
                declared_entrypoint=artifact.declared_entrypoint,
                bundle_hash=artifact.bundle_hash,
                real_order_routing=False,
                payload_json=model_payload(artifact),
                created_at=NOW - timedelta(days=1),
            )
        )


def _successful_execution(artifact, request: ProspectiveRequestEvidenceV1):
    attestation = CandidateRuntimeAttestationV1(
        schema_version="candidate_runtime_attestation_v1",
        isolation_kind="native_windows_codex_sandbox",
        isolation_version="candidate_runtime_v1",
        candidate_artifact_hash=artifact.bundle_hash,
        candidate_tree_hash=artifact.candidate_tree_hash,
        candidate_config_hash=artifact.config_hash,
        candidate_config_files=CONFIG_FILES,
        runtime=artifact.runtime,
        worker_code_hash="f" * 64,
        declared_entrypoint=artifact.declared_entrypoint,
    )
    security = build_candidate_execution_security(
        isolation_kind=attestation.isolation_kind,
        isolation_version=attestation.isolation_version,
        candidate_artifact_hash=artifact.bundle_hash,
        candidate_tree_hash=artifact.candidate_tree_hash,
        runtime_executable_hash=artifact.runtime.executable_sha256,
        worker_code_hash=attestation.worker_code_hash,
        declared_entrypoint=artifact.declared_entrypoint,
        limits=CandidateProcessLimitsV1(
            timeout_seconds=5,
            maximum_stdout_bytes=8192,
            maximum_stderr_bytes=1024,
            maximum_memory_bytes=64 * 1024 * 1024,
            maximum_processes=1,
        ),
    )
    response = build_candidate_decision_response(
        request=request.request,
        targets=(
            CandidateTargetV1(
                symbol="QQQ",
                score=1,
                target_weight=0.5,
            ),
        ),
        diagnostics={"review_due": True},
    )
    return build_successful_execution_evidence(
        request_evidence=request,
        attestation=attestation,
        security=security,
        primary_response=response,
        replay_response=response,
    )


def _seed_additional_parent_decision(
    factory: sessionmaker[Session],
    *,
    suffix: str,
    scheduled_at: datetime,
) -> str:
    cycle_id = f"cycle-persistence-{suffix}"
    decision_id = f"parent-decision-persistence-{suffix}"
    with factory.begin() as session:
        session.add(
            PaperCycleRow(
                cycle_id=cycle_id,
                run_id="parent-run-persistence",
                cycle_kind="Q1_STRATEGIC",
                scheduled_at=scheduled_at,
                data_available_cutoff=scheduled_at,
                status="COMPLETED",
                idempotency_key=cycle_id,
                lease_owner=None,
                lease_expires_at=None,
                attempt_count=1,
                input_manifest_hash=canonical_hash(
                    {"cycle_id": cycle_id, "kind": "input"}
                ),
                output_manifest_hash=canonical_hash(
                    {"cycle_id": cycle_id, "kind": "output"}
                ),
                started_at=scheduled_at,
                completed_at=scheduled_at + timedelta(seconds=5),
                last_error_code=None,
                last_error_detail=None,
                created_at=scheduled_at,
                updated_at=scheduled_at + timedelta(seconds=5),
            )
        )
        session.flush()
        session.add(
            PortfolioDecisionRow(
                portfolio_decision_id=decision_id,
                run_id="parent-run-persistence",
                arm_id="Q1-DET",
                source_cycle_id=cycle_id,
                input_state_sequence=1,
                decision_time=scheduled_at + timedelta(seconds=5),
                algorithm_version="q1_math_core_v1",
                scheduled_at=scheduled_at,
                signal_data_cutoff=scheduled_at,
                portfolio_state_as_of=scheduled_at + timedelta(seconds=1),
                quote_as_of=scheduled_at + timedelta(seconds=1),
                decision_created_at=scheduled_at + timedelta(seconds=5),
                valid_until=scheduled_at + timedelta(minutes=20),
                calendar_session_id="calendar-persistence",
                config_manifest_hash="a" * 64,
                code_version="test-code",
                model_version="test-model",
                source_manifest_hash=canonical_hash(
                    {"decision_id": decision_id, "kind": "source"}
                ),
                input_manifest_hash=canonical_hash(
                    {"decision_id": decision_id, "kind": "input"}
                ),
                payload_json={},
                decision_hash=canonical_hash(
                    {"decision_id": decision_id, "kind": "decision"}
                ),
            )
        )
    return decision_id


def test_prospective_request_and_execution_are_idempotent_append_only(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
) -> None:
    _, engine, factory = sqlite_database
    artifact = _artifact()
    _seed_dependencies(factory, artifact)
    request = _request_evidence(artifact)
    repository = ProspectiveCandidateRepository(factory)

    assert repository.store_request(request)
    assert not repository.store_request(request)
    assert repository.request(request.prospective_request_id) == request

    execution = _successful_execution(artifact, request)

    assert repository.store_execution(execution)
    assert not repository.store_execution(execution)
    assert repository.successful_execution(request.prospective_request_id) == execution
    state = repository.prior_state(
        challenger_id=artifact.challenger_id,
        before_parent_scheduled_at=NOW + timedelta(days=1),
    )
    assert state is not None
    assert state.request == request
    assert state.execution == execution
    assert state.last_review_calendar_session_id == "calendar-persistence"
    status = repository.status()
    assert status["status"] == "PROSPECTIVE_TARGET_RECORDED"
    assert status["request_count"] == 1
    assert status["success_count"] == 1
    assert status["failure_count"] == 0
    assert status["latest"]["targets"] == {
        "QQQ": 0.5,
        "USD_CASH": 0.5,
    }
    assert datetime.fromisoformat(
        status["latest"]["request_recorded_at"]
    ).tzinfo
    assert datetime.fromisoformat(
        status["latest"]["execution_recorded_at"]
    ).tzinfo
    assert status["outcome_status"] == "IMMATURE_FORWARD_ONLY"
    assert status["shadow_started"] is False
    assert status["real_order_routing"] is False

    for table in (
        "research_candidate_prospective_requests",
        "research_candidate_prospective_executions",
    ):
        for statement in (
            f"UPDATE {table} SET payload_json=payload_json",
            f"DELETE FROM {table}",
        ):
            with (
                engine.connect() as connection,
                connection.begin(),
                pytest.raises(DBAPIError, match="append-only"),
            ):
                connection.execute(text(statement))


def test_collector_selects_oldest_decision_without_successful_evidence(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    repository_root,
) -> None:
    _, _, factory = sqlite_database
    artifact = _artifact()
    _seed_dependencies(factory, artifact)
    late = _seed_additional_parent_decision(
        factory,
        suffix="late",
        scheduled_at=NOW + timedelta(days=2),
    )
    early = _seed_additional_parent_decision(
        factory,
        suffix="early",
        scheduled_at=NOW + timedelta(days=1),
    )
    collector = ProspectiveCandidateCollector(
        factory,
        config=load_prospective_candidate_config(
            repository_root / "config"
        ),
    )

    assert collector.next_pending_parent_decision_id(
        parent_run_id="parent-run-persistence",
        challenger_id=artifact.challenger_id,
    ) == "parent-decision-persistence"

    request = _request_evidence(artifact)
    repository = ProspectiveCandidateRepository(factory)
    assert repository.store_request(request)
    assert collector.next_pending_parent_decision_id(
        parent_run_id="parent-run-persistence",
        challenger_id=artifact.challenger_id,
    ) == "parent-decision-persistence"
    assert repository.store_execution(
        _successful_execution(artifact, request)
    )

    assert collector.next_pending_parent_decision_id(
        parent_run_id="parent-run-persistence",
        challenger_id=artifact.challenger_id,
    ) == early
    assert early != late
