"""End-to-end persistence coverage for Candidate discovery registration."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from typer.testing import CliRunner

from trading.cli import app
from trading.domain.hashing import canonical_hash
from trading.domain.q1 import MarketCalendarSession
from trading.persistence.experiment_outcomes import (
    ExperimentOutcomeRepository,
)
from trading.persistence.meta_controller import MetaControllerRepository
from trading.persistence.q1 import MarketCalendarSessionRepository
from trading.persistence.research import ResearchRepository
from trading.persistence.research_scheduler import (
    ResearchSchedulerRepository,
)
from trading.research.candidate_artifact import (
    CandidateRequestBindingV1,
    CandidateRuntimeV1,
    build_candidate_artifact_bundle,
)
from trading.research.candidate_experiment import (
    CandidateExperimentRegistrationService,
)
from trading.research.config import (
    load_research_config,
    meta_controller_parameters,
)
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
    ExperimentInformationRole,
    ExperimentOutcomeEventKind,
    ResearchActionKind,
)
from trading.research.host import build_research_request_v2
from trading.research.meta_controller import build_research_context
from trading.research.v2_contracts import ResearchDecisionV2

NOW = datetime(2026, 7, 27, 18, 0, tzinfo=UTC)
NEW_YORK = ZoneInfo("America/New_York")
CALENDAR_AVAILABLE_AT = NOW + timedelta(minutes=4)


def _catalog() -> AvailableDataCatalogV1:
    payload = {
        "schema_version": "available_data_catalog_v1",
        "catalog_id": "catalog-candidate-discovery",
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


def _proposal() -> AlgorithmProposalV2:
    payload = {
        "schema_version": "algorithm_proposal_v2",
        "proposal_id": "proposal-candidate-discovery",
        "hypothesis_id": "hypothesis-candidate-discovery",
        "hypothesis": "A diversifying sleeve may improve matched outcomes.",
        "economic_mechanism": "The sleeve may diversify correlated errors.",
        "why_current_model_failed": "The parent omits the proposed sleeve.",
        "parent_strategy_id": "Q1-DET",
        "parent_strategy_version": "1.0.0",
        "proposed_strategy_id": "Q1-DET",
        "proposed_strategy_version": "2.0.0",
        "target_horizon": "DAILY",
        "target_universe": ["QQQ"],
        "required_data": ["adjusted_daily_bars"],
        "feature_changes": ["add one bounded sleeve"],
        "signal_formula_changes": [],
        "entry_rule_changes": [],
        "exit_rule_changes": [],
        "position_sizing_changes": [],
        "regime_activation_changes": ["add a bounded gate"],
        "calibration_changes": [],
        "expected_edge_source": "Diversification.",
        "expected_failure_modes": ["NO_EDGE"],
        "invalidation_conditions": ["No positive matched lower bound."],
        "placebo_tests": ["date_shift"],
        "stress_tests": ["cost_3x"],
        "minimum_economic_effect": {"delta_sharpe_lcb": 0.01},
        "estimated_capacity": {"usd": 100000},
        "estimated_turnover": {"annualized": 2.0},
        "estimated_cost_sensitivity": {"cost_3x": 0.0},
        "files_allowed_to_change": [
            "src/trading/strategies/challengers/**",
        ],
        "tests_required": ["tests/candidates/test_candidate.py"],
        "evidence_source_ids": ["evidence-candidate-discovery"],
        "raw_confidence": 0.5,
        "patch_policy_version": "candidate_patch_policy_v2",
        "primary_action_kind": (
            ResearchActionKind.ADD_DIVERSIFYING_SLEEVE
        ),
        "secondary_action_kinds": (
            ResearchActionKind.ADD_REGIME_GATE,
        ),
        "mechanism_tags": ("diversification", "regime"),
        "predicted_portfolio_delta_sharpe": {
            "lower": -0.1,
            "median": 0.06,
            "upper": 0.18,
        },
        "predicted_failure_codes": ("NO_EDGE",),
        "complexity_delta": 1.0,
    }
    return AlgorithmProposalV2.model_validate(
        {**payload, "proposal_hash": canonical_hash(payload)}
    )


def _seed_candidate(
    factory,
    *,
    repository_root: Path,
) -> tuple[str, dict[str, object], datetime]:
    research_config = load_research_config(repository_root / "config")
    outcomes = ExperimentOutcomeRepository(factory)
    snapshot, _ = outcomes.materialize_memory(
        as_of=NOW,
        data_available_cutoff=NOW,
        created_at=NOW,
        persist=True,
    )
    controller = MetaControllerRepository(factory)
    plan, _ = controller.build_plan(
        research_cycle_id="cycle-candidate-discovery",
        snapshot_id=snapshot.snapshot_id,
        context=build_research_context(
            regime_cluster_id="regime-neutral",
            failure_cluster_id="failure-none",
            portfolio_exposure_cluster_id="exposure-balanced",
        ),
        parameters=meta_controller_parameters(research_config),
        config_hash=research_config.manifest_hash,
        available_action_kinds=(
            ResearchActionKind.ADD_DIVERSIFYING_SLEEVE,
            ResearchActionKind.ADD_REGIME_GATE,
        ),
        maximum_total_submissions=2,
        idempotency_key="plan-candidate-discovery",
        generated_at=NOW,
        persist=True,
    )
    research = ResearchRepository(factory)
    selection = research.select_commander(
        ResearchCommanderKind.CODEX_SOL_MAX,
        config_hash=research_config.manifest_hash,
        effective_at=NOW,
        created_at=NOW,
        expected_version=0,
    )
    request = build_research_request_v2(
        outcome_repository=outcomes,
        meta_controller_repository=controller,
        snapshot_id=snapshot.snapshot_id,
        action_plan_id=plan.action_plan_id,
        request_id="request-candidate-discovery",
        research_cycle_id=plan.research_cycle_id,
        commander_selection=selection,
        created_at=NOW,
        as_of=NOW,
        data_available_cutoff=NOW,
        expires_at=NOW + timedelta(hours=2),
        source_snapshot_commit="b" * 40,
        champion_version="1.0.0",
        experiment_family="family-candidate-discovery",
        champion_manifest={"strategy_id": "Q1-DET"},
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
    assert research.create_cycle(request)
    proposal = _proposal()
    decision_payload = {
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
        "decision": ResearchDecisionKind.PROPOSE_STRATEGY_REVISION,
        "rationale": "The bounded proposal is ready for isolated testing.",
        "proposal": proposal,
        "requested_evidence": [],
        "research_memory_snapshot_hash": snapshot.snapshot_hash,
        "research_action_plan_hash": plan.plan_hash,
        "created_at": NOW + timedelta(minutes=1),
    }
    decision = ResearchDecisionV2.model_validate(
        {
            **decision_payload,
            "output_hash": canonical_hash(decision_payload),
        }
    )
    assert research.accept_decision_v2(
        decision,
        received_at=NOW + timedelta(minutes=1),
    ) == proposal.proposal_id

    runtime = CandidateRuntimeV1(
        implementation="CPython",
        version="3.13.12",
        abi_tag="cpython-313",
        executable_sha256="c" * 64,
    )
    test_manifest: dict[str, object] = {
        "schema_version": "candidate_test_manifest_v1",
        "status": "PASSED",
        "exit_code": 0,
        "source_snapshot_hash": "d" * 64,
        "candidate_tree_hash_before": "e" * 64,
        "candidate_tree_hash_after": "e" * 64,
        "patch_hash": "f" * 64,
        "proposal_hash": proposal.proposal_hash,
        "builder_result_hash": "1" * 64,
        "declared_entrypoint": (
            "trading.strategies.challengers.q1_det_v2:decide"
        ),
        "output_limit_exceeded": False,
        "candidate_tree_unchanged": True,
        "candidate_source_projection_unchanged": True,
        "candidate_test_projection_unchanged": True,
        "host_abi_test_unchanged": True,
        "host_principal_persisted": False,
        "raw_output_persisted": False,
        "broker_access_permitted": False,
        "credential_access_permitted": False,
        "network_access_permitted": False,
        "real_order_routing": False,
        "execution_contract_version": (
            "candidate-test-unelevated-workspace-v4"
        ),
        "runtime": runtime.model_dump(mode="python"),
        "test_count": {
            "collected": 13,
            "passed": 13,
            "failed": 0,
            "errors": 0,
        },
    }
    test_manifest_hash = canonical_hash(test_manifest)
    manifest_payload = {
        "schema_version": "challenger_manifest_v1",
        "challenger_id": "challenger-candidate-discovery",
        "strategy_id": proposal.proposed_strategy_id,
        "strategy_version": proposal.proposed_strategy_version,
        "parent_version": proposal.parent_strategy_version,
        "hypothesis_id": proposal.hypothesis_id,
        "experiment_family": request.experiment_family,
        "source_commit": request.source_snapshot_commit,
        "patch_hash": "f" * 64,
        "proposal_hash": proposal.proposal_hash,
        "code_hash": "2" * 64,
        "config_hash": "3" * 64,
        "test_manifest_hash": test_manifest_hash,
        "created_by_commander": request.selected_commander,
        "implemented_by_builder": "CODEX_SOL_MAX",
        "evidence_source_ids": proposal.evidence_source_ids,
        "required_data": proposal.required_data,
        "decision_horizon": proposal.target_horizon,
        "execution_universe": proposal.target_universe,
        "estimated_turnover": proposal.estimated_turnover,
        "estimated_capacity": proposal.estimated_capacity,
        "status": ChallengerStatus.PROPOSED,
        "created_at": NOW + timedelta(minutes=2),
    }
    manifest = ChallengerManifestV1.model_validate(
        {
            **manifest_payload,
            "manifest_hash": canonical_hash(manifest_payload),
        }
    )
    assert research.register_challenger(
        manifest,
        proposal_id=proposal.proposal_id,
    )
    artifact = build_candidate_artifact_bundle(
        bundle_id="candidate-bundle-discovery",
        challenger_id=manifest.challenger_id,
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
        source_snapshot_hash="d" * 64,
        candidate_tree_hash="e" * 64,
        code_hash=manifest.code_hash,
        config_hash=manifest.config_hash,
        patch_hash=manifest.patch_hash,
        proposal_hash=proposal.proposal_hash,
        builder_result_hash="1" * 64,
        test_manifest_hash=test_manifest_hash,
        challenger_manifest_hash=manifest.manifest_hash,
        validation_request_hash="4" * 64,
        runtime=runtime,
        declared_entrypoint=(
            "trading.strategies.challengers.q1_det_v2:decide"
        ),
    )
    artifact_created_at = NOW + timedelta(minutes=3)
    assert research.register_candidate_artifact(
        artifact,
        created_at=artifact_created_at,
    )
    return manifest.challenger_id, test_manifest, artifact_created_at


def _seed_calendar(factory) -> None:
    session_date = date(2026, 7, 29)
    sessions: list[date] = []
    while len(sessions) < 64:
        if session_date.weekday() < 5:
            sessions.append(session_date)
        session_date += timedelta(days=1)
    with factory.begin() as database_session:
        repository = MarketCalendarSessionRepository(database_session)
        for index, current_date in enumerate(sessions):
            open_at = datetime.combine(
                current_date,
                time(9, 30),
                tzinfo=NEW_YORK,
            ).astimezone(UTC)
            close_at = datetime.combine(
                current_date,
                time(16, 0),
                tzinfo=NEW_YORK,
            ).astimezone(UTC)
            source_hash = canonical_hash(
                {"date": current_date, "source": "test-calendar"}
            )
            session_hash = canonical_hash(
                {
                    "date": current_date,
                    "open_at": open_at,
                    "close_at": close_at,
                    "source_hash": source_hash,
                }
            )
            repository.append(
                MarketCalendarSession(
                    calendar_session_id=f"calendar-discovery-{index:03d}",
                    calendar_version="alpaca_market_calendar_v1",
                    session_date=current_date,
                    open_at=open_at,
                    close_at=close_at,
                    source="ALPACA_CALENDAR_API_PIT",
                    available_at=CALENDAR_AVAILABLE_AT,
                    session_hash=session_hash,
                    created_at=CALENDAR_AVAILABLE_AT,
                    config_manifest_hash="5" * 64,
                    code_version="test",
                    model_version="calendar-test-v1",
                    source_manifest_hash=source_hash,
                )
            )


def test_candidate_discovery_registration_is_idempotent_and_non_promoting(
    sqlite_database,
    repository_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    decision_now = NOW + timedelta(minutes=5)
    monkeypatch.setattr(
        "trading.persistence.experiment_outcomes._database_now",
        lambda _session: decision_now,
    )
    database_url, _, factory = sqlite_database
    challenger_id, test_manifest, artifact_created_at = _seed_candidate(
        factory,
        repository_root=repository_root,
    )
    _seed_calendar(factory)
    research = ResearchRepository(factory)
    outcomes = ExperimentOutcomeRepository(factory)
    service = CandidateExperimentRegistrationService(
        research_repository=research,
        outcome_repository=outcomes,
        scheduler_repository=ResearchSchedulerRepository(factory),
        config=load_research_config(repository_root / "config"),
    )

    first = service.register_discovery(
        challenger_id=challenger_id,
        test_manifest=test_manifest,
    )
    repeated = service.register_discovery(
        challenger_id=challenger_id,
        test_manifest=test_manifest,
    )

    assert first.action_created is True
    assert first.registration_event_created is True
    assert first.technical_event_created is True
    assert repeated.action_created is False
    assert repeated.registration_event_created is False
    assert repeated.technical_event_created is False
    assert repeated.action.action_hash == first.action.action_hash
    assert repeated.registration_event.event_hash == (
        first.registration_event.event_hash
    )
    assert repeated.technical_event.event_hash == (
        first.technical_event.event_hash
    )
    assert first.action.created_at >= artifact_created_at
    assert first.action.decision_at == first.action.created_at
    assert first.action.decision_at >= CALENDAR_AVAILABLE_AT
    assert first.action.information_role is ExperimentInformationRole.DISCOVERY
    assert first.action.meta_training_permitted is False
    assert len(first.maturity_calendar_session_ids) == 63
    assert first.technical_event.event_kind is (
        ExperimentOutcomeEventKind.TECHNICAL_OUTCOME_RECORDED
    )
    assert first.technical_event.technical_success is True
    assert first.technical_event.eligible_for_meta_training is False
    assert len(outcomes.event_chain(first.experiment_id)) == 2
    assert research.challenger_status(challenger_id) is (
        ChallengerStatus.PROPOSED
    )
    status = research.status()
    assert status["shadow_arm_registrations"] == []
    assert status["promotion_decisions"] == []
    discovery = status["experiment_outcome_ledger"][
        "latest_discovery_registration"
    ]
    assert discovery["status"] == "DISCOVERY_TECHNICAL_ATTESTED"
    assert discovery["meta_training_permitted"] is False
    assert status["real_order_routing"] is False

    manifest_path = tmp_path / "candidate-test-manifest.json"
    manifest_path.write_text(
        json.dumps(test_manifest),
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "TRADING_CONFIG_DIR",
        str(repository_root / "config"),
    )
    monkeypatch.setenv("TRADING_RAW_STORE", str(tmp_path / "raw"))
    monkeypatch.setenv("TRADING_REAL_BROKER_ENABLED", "false")
    monkeypatch.setenv("TRADING_PRODUCTION_UNLOCK", "false")
    monkeypatch.setenv("TRADING_Q1_ALPACA_PAPER_ENABLED", "false")
    cli_result = CliRunner().invoke(
        app,
        [
            "research",
            "outcome",
            "register-candidate",
            "--challenger-id",
            challenger_id,
            "--test-manifest",
            str(manifest_path),
        ],
    )
    assert cli_result.exit_code == 0, cli_result.output
    cli_payload = json.loads(cli_result.stdout)
    assert cli_payload["action_created"] is False
    assert cli_payload["technical_event_created"] is False
    assert cli_payload["meta_training_permitted"] is False
    assert cli_payload["challenger_status_advanced"] is False
    assert cli_payload["shadow_started"] is False
    assert cli_payload["real_order_routing"] is False
