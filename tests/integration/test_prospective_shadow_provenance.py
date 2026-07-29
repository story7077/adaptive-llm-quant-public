from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import model_payload
from trading.domain.hashing import canonical_hash
from trading.domain.q1 import Q1DecisionInputManifest, Q1StrategyDecision
from trading.persistence.models import (
    AlgorithmProposalRow,
    ChallengerEventRow,
    ChallengerManifestRow,
    DomainEventRow,
    MarketBarRow,
    MarketCalendarSessionRow,
    MarketQuoteRow,
    MarketStreamStatusRow,
    OosLockboxResultRow,
    PaperCycleRow,
    PortfolioDecisionRow,
    ResearchCandidateArtifactRow,
    ResearchCandidateProspectiveExecutionRow,
    ResearchCandidateProspectiveRequestRow,
    ResearchCommanderSelectionRow,
    ResearchCycleRow,
    ResearchShadowArmRegistrationRow,
    RunRow,
    StrategyEvaluationAnchorRow,
)
from trading.persistence.prospective import ProspectiveCandidateRepository
from trading.persistence.research import ResearchRepository
from trading.persistence.research_shadow import (
    ResearchShadowInitialization,
    ResearchShadowRuntimeRepository,
)
from trading.research.candidate_abi import (
    CandidateEvaluationVariantV1,
    CandidateFeatureValueV1,
    CandidateInstrumentInputV1,
    CandidateTargetV1,
    build_candidate_decision_request,
    build_candidate_decision_response,
)
from trading.research.candidate_artifact import CandidateRuntimeV1
from trading.research.candidate_process import (
    CandidateProcessLimitsV1,
    build_candidate_execution_security,
)
from trading.research.commander_candidate import (
    CandidateConfigFileAttestationV1,
    CandidateRuntimeAttestationV1,
)
from trading.research.config import (
    load_research_config,
    shadow_paper_parameters,
)
from trading.research.oos_shadow_operations import (
    MatchedShadowCycleCommitV1,
    OosShadowOperationError,
    TrustedOosShadowOperations,
)
from trading.research.prospective import (
    ProspectiveRequestEvidenceV1,
    ProspectiveSourceBarV1,
    ProspectiveSourceManifestV1,
    build_successful_execution_evidence,
    load_prospective_candidate_config,
)
from trading.research.prospective_shadow import (
    PROSPECTIVE_SHADOW_SOURCE_AGGREGATE,
    TRUSTED_SHADOW_CYCLE_EVENT,
)
from trading.research.shadow import ShadowExecutionContract
from trading.runtime.prospective_shadow import (
    ProspectiveShadowOperationError,
    TrustedProspectiveShadowOperations,
)

CHALLENGER_ID = "challenger-prospective-shadow"
ARTIFACT_HASH = "a" * 64
BAD_ARTIFACT_HASH = "0" * 64
CONFIG_FILES: tuple[CandidateConfigFileAttestationV1, ...] = (
    CandidateConfigFileAttestationV1(
        path="config/strategies/challengers/q1-det-v2.0.0.yaml",
        sha256="b" * 64,
    ),
)
CONFIG_HASH = canonical_hash([model_payload(item) for item in CONFIG_FILES])
MARKET_HASH = "c" * 64
PARENT_RUN_ID = "parent-run-prospective-shadow"
PARENT_DECISION_ID = "parent-decision-prospective-shadow"


def _business_dates(through: date, count: int) -> tuple[date, ...]:
    values: list[date] = []
    cursor = through
    while len(values) < count:
        if cursor.weekday() < 5:
            values.append(cursor)
        cursor -= timedelta(days=1)
    return tuple(sorted(values))


def _execution_contract() -> ShadowExecutionContract:
    return ShadowExecutionContract(
        market_input_manifest_hash=MARKET_HASH,
        decision_schedule_version="schedule-v1",
        execution_scenario_version="execution-v1",
        cost_model_version="cost-v1",
        starting_capital_usd="100000.00",
        liquidity_policy_version="liquidity-v1",
    )


def _q1_decision(
    *,
    decision_time: datetime,
    calendar_session_id: str,
) -> Q1StrategyDecision:
    versioned = {
        "algorithm_version": "q1_math_core_v1",
        "config_manifest_hash": "1" * 64,
        "code_version": "code-v1",
        "model_version": "q1_runtime_v1",
        "source_manifest_hash": "2" * 64,
    }
    input_payload = {
        **versioned,
        "calendar_session_id": calendar_session_id,
        "source_bars": (),
        "quotes": (),
    }
    manifest = Q1DecisionInputManifest.model_validate(
        {
            **input_payload,
            "manifest_hash": canonical_hash(input_payload),
        }
    )
    payload = {
        **versioned,
        "portfolio_decision_id": PARENT_DECISION_ID,
        "run_id": PARENT_RUN_ID,
        "arm_id": "Q1-DET",
        "source_cycle_id": "parent-cycle-prospective-shadow",
        "input_state_sequence": 1,
        "decision_kind": "STRATEGIC_TARGET",
        "scheduled_at": decision_time,
        "signal_data_cutoff": decision_time - timedelta(minutes=1),
        "portfolio_state_as_of": decision_time,
        "quote_as_of": decision_time,
        "decision_created_at": decision_time,
        "valid_until": decision_time + timedelta(minutes=20),
        "input_manifest": manifest,
        "target_weights": {
            "QQQ": Decimal("0.40"),
            "SOXX": Decimal("0.20"),
            "USD_CASH": Decimal("0.40"),
        },
        "diagnostics": {},
        "worker_fence_token": "parent-worker-fence",
        "cycle_attempt_count": 1,
    }
    return Q1StrategyDecision.model_validate(
        {**payload, "decision_hash": canonical_hash(payload)}
    )


def _seed_research_and_shadow(
    factory: sessionmaker[Session],
    *,
    repository_root: Path,
    created_at: datetime,
    oos_artifact_hash: str = ARTIFACT_HASH,
) -> ResearchShadowInitialization:
    research_config = load_research_config(repository_root / "config")
    contract = _execution_contract()
    contract_payload = {
        "market_input_manifest_hash": contract.market_input_manifest_hash,
        "decision_schedule_version": contract.decision_schedule_version,
        "execution_scenario_version": contract.execution_scenario_version,
        "cost_model_version": contract.cost_model_version,
        "starting_capital_usd": contract.starting_capital_usd,
        "liquidity_policy_version": contract.liquidity_policy_version,
    }
    contract_hash = canonical_hash(contract_payload)
    with factory.begin() as session:
        session.add(
            ResearchCommanderSelectionRow(
                selection_id="selection-prospective-shadow",
                version=1,
                selected_commander="CODEX_SOL_MAX",
                effective_at=created_at,
                config_hash="3" * 64,
                payload_json={},
                created_at=created_at,
            )
        )
        session.flush()
        session.add(
            ResearchCycleRow(
                research_cycle_id="research-cycle-prospective-shadow",
                request_id="research-request-prospective-shadow",
                selection_id="selection-prospective-shadow",
                selection_version=1,
                selected_commander="CODEX_SOL_MAX",
                source_snapshot_commit="4" * 64,
                champion_version="1.0.0",
                experiment_family="prospective-shadow",
                as_of=created_at,
                data_available_cutoff=created_at,
                expires_at=created_at + timedelta(days=1),
                context_manifest_hash="5" * 64,
                request_hash="6" * 64,
                payload_json={},
                created_at=created_at,
            )
        )
        session.flush()
        session.add(
            AlgorithmProposalRow(
                proposal_id="proposal-prospective-shadow",
                research_cycle_id="research-cycle-prospective-shadow",
                hypothesis_id="hypothesis-prospective-shadow",
                parent_strategy_id="Q1-DET",
                parent_strategy_version="1.0.0",
                proposed_strategy_id="Q1-DET",
                proposed_strategy_version="2.0.0",
                proposal_hash="7" * 64,
                evidence_manifest_hash="8" * 64,
                payload_json={},
                created_at=created_at,
            )
        )
        session.flush()
        session.add(
            ChallengerManifestRow(
                challenger_id=CHALLENGER_ID,
                proposal_id="proposal-prospective-shadow",
                strategy_id="Q1-DET",
                strategy_version="2.0.0",
                parent_version="1.0.0",
                experiment_family="prospective-shadow",
                source_commit="4" * 64,
                patch_hash="9" * 64,
                code_hash="d" * 64,
                config_hash=CONFIG_HASH,
                test_manifest_hash="e" * 64,
                initial_status="PROPOSED",
                manifest_hash="f" * 64,
                payload_json={},
                created_at=created_at,
            )
        )
        session.flush()
        session.add(
            ResearchCandidateArtifactRow(
                bundle_id="bundle-prospective-shadow",
                challenger_id=CHALLENGER_ID,
                proposal_id="proposal-prospective-shadow",
                research_cycle_id="research-cycle-prospective-shadow",
                candidate_tree_hash="1" * 64,
                code_hash="d" * 64,
                config_hash=CONFIG_HASH,
                test_manifest_hash="e" * 64,
                declared_entrypoint="candidate.strategy:decide",
                bundle_hash=ARTIFACT_HASH,
                real_order_routing=False,
                payload_json={},
                created_at=created_at,
            )
        )
        session.add(
            OosLockboxResultRow(
                oos_result_id="oos-prospective-shadow",
                challenger_id=CHALLENGER_ID,
                experiment_family="prospective-shadow",
                submission_number=1,
                candidate_artifact_hash=oos_artifact_hash,
                evaluation_contract_hash="2" * 64,
                verdict="PASS",
                common_sessions=126,
                result_hash="3" * 64,
                payload_json={},
                evaluated_at=created_at,
                created_at=created_at,
            )
        )
        session.flush()
        for role, arm_id, version in (
            ("CHAMPION", "q1-champion", "1.0.0"),
            ("CHALLENGER", "q1-challenger", "2.0.0"),
        ):
            session.add(
                ResearchShadowArmRegistrationRow(
                    shadow_registration_id=f"registration-{role.lower()}-prospective",
                    shadow_pair_id="pair-prospective-shadow",
                    challenger_id=CHALLENGER_ID,
                    oos_result_id="oos-prospective-shadow",
                    arm_role=role,
                    arm_id=arm_id,
                    strategy_id="Q1-DET",
                    strategy_version=version,
                    execution_contract_hash=contract_hash,
                    real_order_routing=False,
                    payload_json={"execution_contract": contract_payload},
                    created_at=created_at,
                )
            )
        session.add(
            ChallengerEventRow(
                challenger_event_id="pending-prospective-shadow",
                challenger_id=CHALLENGER_ID,
                sequence=1,
                from_status="PROPOSED",
                to_status="SHADOW_PENDING",
                reason_code="OOS_PASS_SHADOW_REGISTERED",
                artifact_hash="3" * 64,
                idempotency_key="pending-prospective-shadow",
                event_hash="4" * 64,
                payload_json={},
                created_at=created_at,
            )
        )
    ResearchRepository(factory).start_shadow_evaluation(
        challenger_id=CHALLENGER_ID,
        idempotency_key="start-prospective-shadow",
        created_at=created_at,
    )
    return ResearchShadowRuntimeRepository(factory).initialize_from_lifecycle(
        challenger_id=CHALLENGER_ID,
        champion_artifact_hash="5" * 64,
        paper_parameters=shadow_paper_parameters(research_config),
        code_version="code-v1",
        created_at=created_at,
    )


def _seed_parent_and_prospective(
    factory: sessionmaker[Session],
    *,
    repository_root: Path,
    decision_time: datetime,
    candidate_artifact_bundle_id: str = "bundle-prospective-shadow",
    candidate_artifact_hash: str = ARTIFACT_HASH,
) -> tuple[str, datetime]:
    config = load_prospective_candidate_config(repository_root / "config")
    calendar_session_id = "calendar-prospective-shadow"
    parent = _q1_decision(
        decision_time=decision_time,
        calendar_session_id=calendar_session_id,
    )
    completed_dates = _business_dates(decision_time.date() - timedelta(days=1), 20)
    source_bars = tuple(
        ProspectiveSourceBarV1(
            bar_id=f"bar-{symbol.lower()}-{session_date.isoformat()}",
            symbol=symbol,
            session_date=session_date,
            source_event_time=datetime.combine(
                session_date,
                time(20, 0),
                tzinfo=UTC,
            ),
            available_at=datetime.combine(
                session_date,
                time(20, 1),
                tzinfo=UTC,
            ),
            payload_hash=canonical_hash(
                ("prospective-shadow-bar", symbol, session_date.isoformat())
            ),
        )
        for symbol in config.config.reference_universe
        for session_date in completed_dates
    )
    source_payload = {
        "schema_version": "candidate_prospective_source_manifest_v1",
        "producer_version": config.config.producer_version,
        "challenger_id": CHALLENGER_ID,
        "candidate_artifact_hash": candidate_artifact_hash,
        "parent_run_id": PARENT_RUN_ID,
        "parent_portfolio_decision_id": PARENT_DECISION_ID,
        "parent_decision_hash": parent.decision_hash,
        "parent_input_manifest_hash": parent.input_manifest.manifest_hash,
        "parent_scheduled_at": parent.scheduled_at,
        "evaluation_anchor_id": "anchor-prospective-shadow",
        "evaluation_anchor_hash": "6" * 64,
        "prior_prospective_request_id": None,
        "prior_execution_hash": None,
        "state_source": "CASH_ONLY_AT_EVALUATION_ANCHOR",
        "market_dataset_version": config.config.market_data.dataset_version,
        "signal_data_cutoff": decision_time,
        "completed_session_dates": completed_dates,
        "source_bars": source_bars,
        "formula_contract_hash": "7" * 64,
        "host_config_manifest_hash": config.manifest_hash,
    }
    source = ProspectiveSourceManifestV1.model_validate(
        {**source_payload, "manifest_hash": canonical_hash(source_payload)}
    )
    latest_by_symbol = {
        symbol: next(
            item
            for item in reversed(source_bars)
            if item.symbol == symbol
        )
        for symbol in config.config.reference_universe
    }
    instruments = tuple(
        CandidateInstrumentInputV1(
            symbol=symbol,
            current_weight=0,
            membership_available_at=decision_time - timedelta(days=365),
            membership_valid_from=decision_time - timedelta(days=365),
            membership_valid_until=None,
            instrument_is_non_survivor=False,
            features=(
                CandidateFeatureValueV1(
                    name="signal",
                    value=1,
                    source_event_time=latest_by_symbol[symbol].source_event_time,
                    available_at=latest_by_symbol[symbol].available_at,
                    source_revision=0,
                    revision_available_at=latest_by_symbol[symbol].available_at,
                    revision_was_known_at_cutoff=True,
                    source_hash=latest_by_symbol[symbol].payload_hash,
                ),
            ),
        )
        for symbol in config.config.reference_universe
    )
    candidate_request = build_candidate_decision_request(
        request_id="prospective-request-shadow",
        challenger_id=CHALLENGER_ID,
        candidate_artifact_hash=candidate_artifact_hash,
        strategy_id=config.config.strategy_id,
        strategy_version=config.config.strategy_version,
        decision_time=decision_time,
        signal_data_cutoff=decision_time,
        variant=CandidateEvaluationVariantV1(),
        instruments=instruments,
        constraints=config.config.constraints,
        strategy_parameters=config.strategy_parameters,
        source_data_manifest_hash=source.manifest_hash,
    )
    evidence_payload = {
        "schema_version": "candidate_prospective_request_evidence_v1",
        "prospective_request_id": candidate_request.request_id,
        "challenger_id": CHALLENGER_ID,
        "candidate_artifact_bundle_id": candidate_artifact_bundle_id,
        "candidate_artifact_hash": candidate_artifact_hash,
        "candidate_config_hash": CONFIG_HASH,
        "strategy_config_content_sha256": (
            config.config.strategy_config_content_sha256
        ),
        "parent_run_id": PARENT_RUN_ID,
        "parent_portfolio_decision_id": PARENT_DECISION_ID,
        "parent_scheduled_at": parent.scheduled_at,
        "calendar_session_id": calendar_session_id,
        "evaluation_anchor_id": "anchor-prospective-shadow",
        "prior_prospective_request_id": None,
        "source_manifest": source,
        "request": candidate_request,
        "created_at": decision_time,
        "real_order_routing": False,
        "automatic_promotion_enabled": False,
        "challenger_lifecycle_advance_enabled": False,
        "shadow_activation_enabled": False,
    }
    evidence = ProspectiveRequestEvidenceV1.model_validate(
        {
            **evidence_payload,
            "evidence_hash": canonical_hash(evidence_payload),
        }
    )
    with factory.begin() as session:
        session.add(
            RunRow(
                run_id=PARENT_RUN_ID,
                mode="PAPER",
                experiment_version="q1_math_core_v1",
                config_manifest_hash="1" * 64,
                code_commit="code-v1",
                started_at=decision_time - timedelta(hours=1),
                ended_at=None,
                status="RUNNING",
                result_manifest=None,
                result_hash=None,
            )
        )
        session.add(
            MarketCalendarSessionRow(
                calendar_session_id=calendar_session_id,
                algorithm_version="q1_math_core_v1",
                calendar_version="alpaca_market_calendar_v1",
                session_date=decision_time.date(),
                open_at=decision_time - timedelta(minutes=30),
                close_at=decision_time + timedelta(hours=6),
                source="TEST",
                available_at=decision_time - timedelta(days=1),
                config_manifest_hash="1" * 64,
                code_version="code-v1",
                model_version="q1_runtime_v1",
                source_manifest_hash="2" * 64,
                session_hash="8" * 64,
                payload_json={},
                created_at=decision_time - timedelta(days=1),
            )
        )
        session.flush()
        session.add(
            PaperCycleRow(
                cycle_id=parent.source_cycle_id,
                run_id=PARENT_RUN_ID,
                cycle_kind="Q1_STRATEGIC",
                scheduled_at=decision_time,
                data_available_cutoff=parent.signal_data_cutoff,
                status="COMPLETED",
                idempotency_key="parent-cycle-prospective-shadow",
                lease_owner=None,
                lease_expires_at=None,
                attempt_count=1,
                input_manifest_hash=parent.input_manifest.manifest_hash,
                output_manifest_hash=parent.decision_hash,
                started_at=decision_time,
                completed_at=decision_time,
                last_error_code=None,
                last_error_detail=None,
                created_at=decision_time,
                updated_at=decision_time,
            )
        )
        session.flush()
        session.add(
            PortfolioDecisionRow(
                portfolio_decision_id=parent.portfolio_decision_id,
                run_id=parent.run_id,
                arm_id=parent.arm_id.value,
                source_cycle_id=parent.source_cycle_id,
                input_state_sequence=parent.input_state_sequence,
                decision_time=parent.decision_created_at,
                algorithm_version=parent.algorithm_version,
                scheduled_at=parent.scheduled_at,
                signal_data_cutoff=parent.signal_data_cutoff,
                portfolio_state_as_of=parent.portfolio_state_as_of,
                quote_as_of=parent.quote_as_of,
                decision_created_at=parent.decision_created_at,
                valid_until=parent.valid_until,
                calendar_session_id=calendar_session_id,
                config_manifest_hash=parent.config_manifest_hash,
                code_version=parent.code_version,
                model_version=parent.model_version,
                source_manifest_hash=parent.source_manifest_hash,
                input_manifest_hash=parent.input_manifest.manifest_hash,
                payload_json=model_payload(parent),
                decision_hash=parent.decision_hash,
            )
        )
        session.add(
            StrategyEvaluationAnchorRow(
                evaluation_anchor_id="anchor-prospective-shadow",
                run_id=PARENT_RUN_ID,
                algorithm_version="q1_math_core_v1",
                calendar_session_id=calendar_session_id,
                common_t0_at=decision_time - timedelta(minutes=5),
                initial_nav_usd=Decimal("100000"),
                quote_manifest_hash="9" * 64,
                config_manifest_hash="1" * 64,
                code_version="code-v1",
                model_version="q1_runtime_v1",
                source_manifest_hash="2" * 64,
                anchor_hash="6" * 64,
                payload_json={},
                created_at=decision_time - timedelta(minutes=5),
            )
        )
        for item in source_bars:
            session.add(
                MarketBarRow(
                    bar_id=item.bar_id,
                    provider=config.config.market_data.provider,
                    feed=config.config.market_data.feed,
                    symbol=item.symbol,
                    timeframe=config.config.market_data.timeframe,
                    event_time=item.source_event_time,
                    provider_timestamp=item.source_event_time.isoformat(),
                    available_at=item.available_at,
                    ingested_at=item.available_at,
                    source_kind="HISTORICAL",
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100"),
                    volume=Decimal("1000000"),
                    vwap=Decimal("100"),
                    trade_count=1000,
                    request_id=None,
                    payload_hash=item.payload_hash,
                    raw_object_uri=None,
                    payload_json={
                        "_adjustment": config.config.market_data.adjustment,
                        "_dataset_version": config.config.market_data.dataset_version,
                    },
                )
            )
    repository = ProspectiveCandidateRepository(factory)
    assert repository.store_request(evidence) is True
    response = build_candidate_decision_response(
        request=candidate_request,
        targets=tuple(
            CandidateTargetV1(
                symbol=symbol,
                score=1 if symbol in {"QQQ", "SOXX"} else 0,
                target_weight=(
                    0.45
                    if symbol == "QQQ"
                    else 0.25
                    if symbol == "SOXX"
                    else 0
                ),
            )
            for symbol in config.config.reference_universe
        ),
        diagnostics={"source": "isolated-primary-and-replay"},
    )
    attestation = CandidateRuntimeAttestationV1(
        schema_version="candidate_runtime_attestation_v1",
        isolation_kind="native_windows_codex_sandbox",
        isolation_version="candidate_runtime_v1",
        candidate_artifact_hash=candidate_artifact_hash,
        candidate_tree_hash="1" * 64,
        candidate_config_hash=CONFIG_HASH,
        candidate_config_files=CONFIG_FILES,
        runtime=CandidateRuntimeV1(
            implementation="CPython",
            version="3.13.12",
            abi_tag="cpython-313",
            executable_sha256="a" * 64,
        ),
        worker_code_hash="b" * 64,
        declared_entrypoint="candidate.strategy:decide",
    )
    security = build_candidate_execution_security(
        isolation_kind=attestation.isolation_kind,
        isolation_version=attestation.isolation_version,
        candidate_artifact_hash=candidate_artifact_hash,
        candidate_tree_hash=attestation.candidate_tree_hash,
        runtime_executable_hash=attestation.runtime.executable_sha256,
        worker_code_hash=attestation.worker_code_hash,
        declared_entrypoint=attestation.declared_entrypoint,
        limits=CandidateProcessLimitsV1(
            timeout_seconds=5,
            maximum_stdout_bytes=8192,
            maximum_stderr_bytes=1024,
            maximum_memory_bytes=64 * 1024 * 1024,
            maximum_processes=1,
        ),
    )
    execution = build_successful_execution_evidence(
        request_evidence=evidence,
        attestation=attestation,
        security=security,
        primary_response=response,
        replay_response=response,
    )
    assert repository.store_execution(execution) is True
    with factory() as session:
        request_row = session.get(
            ResearchCandidateProspectiveRequestRow,
            evidence.prospective_request_id,
        )
        execution_row = session.get(
            ResearchCandidateProspectiveExecutionRow,
            execution.execution_id,
        )
        assert request_row is not None
        assert execution_row is not None
        available_at = max(
            request_row.recorded_at.replace(tzinfo=UTC),
            execution_row.recorded_at.replace(tzinfo=UTC),
            decision_time,
        )
    return evidence.prospective_request_id, available_at


def _seed_quotes(
    factory: sessionmaker[Session],
    *,
    repository_root: Path,
    available_at: datetime,
) -> datetime:
    config = load_prospective_candidate_config(repository_root / "config")
    quote_time = available_at + timedelta(seconds=1)
    quote_as_of = quote_time + timedelta(seconds=1)
    with factory.begin() as session:
        session.add(
            MarketStreamStatusRow(
                provider=config.config.market_data.provider,
                feed=config.config.market_data.feed,
                state="CONNECTED",
                connected_at=quote_time,
                disconnected_at=None,
                last_message_at=quote_time,
                last_bar_at=None,
                last_quote_at=quote_time,
                last_trade_at=None,
                reconnect_count=0,
                consecutive_failures=0,
                last_error_code=None,
                last_error_detail=None,
                updated_at=quote_time,
            )
        )
        for symbol, midpoint in (
            ("QQQ", Decimal("500")),
            ("SOXX", Decimal("250")),
        ):
            source_hash = canonical_hash(("quote", symbol, quote_time.isoformat()))
            session.add(
                MarketQuoteRow(
                    quote_id=f"quote-{symbol.lower()}-prospective-shadow",
                    provider=config.config.market_data.provider,
                    feed=config.config.market_data.feed,
                    symbol=symbol,
                    event_time=quote_time,
                    provider_timestamp=quote_time.isoformat(),
                    available_at=quote_time,
                    ingested_at=quote_time,
                    source_kind="STREAM",
                    bid_exchange="V",
                    bid_price=midpoint - Decimal("0.05"),
                    bid_size_round_lots=1000,
                    ask_exchange="V",
                    ask_price=midpoint + Decimal("0.05"),
                    ask_size_round_lots=1000,
                    conditions=[],
                    tape="C",
                    payload_hash=source_hash,
                    raw_object_uri=None,
                    payload_json={},
                )
            )
    return quote_as_of


def _seed_complete_bridge(
    factory: sessionmaker[Session],
    repository_root: Path,
) -> tuple[ResearchShadowInitialization, str, datetime]:
    now = datetime.now(UTC).replace(microsecond=0)
    initialized = _seed_research_and_shadow(
        factory,
        repository_root=repository_root,
        created_at=now - timedelta(minutes=5),
    )
    request_id, available_at = _seed_parent_and_prospective(
        factory,
        repository_root=repository_root,
        decision_time=now - timedelta(minutes=1),
    )
    quote_as_of = _seed_quotes(
        factory,
        repository_root=repository_root,
        available_at=available_at,
    )
    return initialized, request_id, quote_as_of


def test_host_derives_targets_and_atomically_persists_source_provenance(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    initialized, request_id, quote_as_of = _seed_complete_bridge(
        factory,
        repository_root,
    )
    service = TrustedProspectiveShadowOperations(
        factory,
        prospective_config=load_prospective_candidate_config(
            repository_root / "config"
        ),
        clock=lambda: quote_as_of,
    )
    assert (
        service.next_eligible_request_id(
            run_id=initialized.spec.run_id,
            as_of=quote_as_of,
        )
        == request_id
    )
    prepared, preview = service.preview(
        run_id=initialized.spec.run_id,
        prospective_request_id=request_id,
        as_of=quote_as_of,
    )
    assert prepared.champion_target.weight_map() == {
        "QQQ": Decimal("0.40"),
        "SOXX": Decimal("0.20"),
        "USD_CASH": Decimal("0.40"),
    }
    assert prepared.challenger_target.weight_map() == {
        "QQQ": Decimal("0.45"),
        "SOXX": Decimal("0.25"),
        "USD_CASH": Decimal("0.30"),
    }
    assert (
        prepared.provenance.primary_response_hash
        == prepared.provenance.replay_response_hash
    )
    assert set(prepared.provenance.quote_id_by_symbol) == {"QQQ", "SOXX"}
    assert all(
        len(bar_ids) == 20
        for bar_ids in prepared.provenance.adv_source_bar_ids_by_symbol.values()
    )
    committed = service.commit(
        run_id=initialized.spec.run_id,
        prospective_request_id=request_id,
        as_of=quote_as_of,
    )
    repeated = service.commit(
        run_id=initialized.spec.run_id,
        prospective_request_id=request_id,
        as_of=quote_as_of,
    )
    assert committed.cycle.result_hash == preview.result_hash
    assert repeated.cycle.result_hash == committed.cycle.result_hash
    assert repeated.replay_hash == committed.replay_hash
    assert (
        service.next_eligible_request_id(
            run_id=initialized.spec.run_id,
            as_of=quote_as_of,
        )
        is None
    )
    with factory() as session:
        cycle_event = session.scalar(
            select(DomainEventRow).where(
                DomainEventRow.aggregate_type
                == "RESEARCH_MATCHED_SHADOW_CYCLE"
            )
        )
        assert cycle_event is not None
        assert cycle_event.event_type == TRUSTED_SHADOW_CYCLE_EVENT
        assert cycle_event.causation_id == prepared.provenance.provenance_id
        source_event = session.get(
            DomainEventRow,
            prepared.provenance.provenance_id,
        )
        assert source_event is not None
        assert (
            source_event.aggregate_type
            == PROSPECTIVE_SHADOW_SOURCE_AGGREGATE
        )
        assert source_event.payload_hash == prepared.provenance.provenance_hash
    trust = ResearchShadowRuntimeRepository(factory).source_trust_status(
        initialized.spec.run_id
    )
    assert trust["trusted_cycle_count"] == 1
    assert trust["unattested_cycle_count"] == 0
    assert trust["source_provenance_ready"] is True
    assert trust["manual_cycle_commit_enabled"] is False


def test_manual_target_json_cannot_commit_promotion_facing_shadow_performance(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    initialized, request_id, quote_as_of = _seed_complete_bridge(
        factory,
        repository_root,
    )
    prospective = TrustedProspectiveShadowOperations(
        factory,
        prospective_config=load_prospective_candidate_config(
            repository_root / "config"
        ),
        clock=lambda: quote_as_of,
    )
    prepared, _ = prospective.preview(
        run_id=initialized.spec.run_id,
        prospective_request_id=request_id,
        as_of=quote_as_of,
    )
    payload = {
        "schema_version": "matched_shadow_cycle_commit_v1",
        "input_id": "manual-cycle-forbidden",
        "run_id": initialized.spec.run_id,
        "champion_target": prepared.champion_target,
        "challenger_target": prepared.challenger_target,
        "quote_bundle": prepared.quote_bundle,
        "created_at": quote_as_of,
        "automatic_promotion_enabled": False,
        "real_order_routing": False,
    }
    request = MatchedShadowCycleCommitV1.model_validate(
        {**payload, "input_hash": canonical_hash(payload)}
    )
    with pytest.raises(
        OosShadowOperationError,
        match="UNATTESTED_MANUAL_SHADOW_CYCLE_COMMIT_DISABLED",
    ):
        TrustedOosShadowOperations(
            factory,
            config=load_research_config(repository_root / "config"),
            clock=lambda: quote_as_of,
        ).commit_cycle(request)
    assert (
        ResearchShadowRuntimeRepository(factory)
        .source_trust_status(initialized.spec.run_id)["trusted_cycle_count"]
        == 0
    )


def test_pre_activation_prospective_evidence_is_not_shadow_eligible(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    now = datetime.now(UTC).replace(microsecond=0)
    initialized = _seed_research_and_shadow(
        factory,
        repository_root=repository_root,
        created_at=now + timedelta(minutes=5),
    )
    request_id, _ = _seed_parent_and_prospective(
        factory,
        repository_root=repository_root,
        decision_time=now - timedelta(minutes=1),
    )
    service = TrustedProspectiveShadowOperations(
        factory,
        prospective_config=load_prospective_candidate_config(
            repository_root / "config"
        ),
        clock=lambda: now,
    )
    assert (
        service.next_eligible_request_id(
            run_id=initialized.spec.run_id,
            as_of=now,
        )
        is None
    )
    with pytest.raises(
        ProspectiveShadowOperationError,
        match="PROSPECTIVE_SHADOW_SOURCE_BINDING_MISMATCH",
    ):
        service.prepare(
            run_id=initialized.spec.run_id,
            prospective_request_id=request_id,
            as_of=now,
        )


def test_candidate_artifact_mismatch_cannot_enter_shadow_runtime(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    now = datetime.now(UTC).replace(microsecond=0)
    initialized = _seed_research_and_shadow(
        factory,
        repository_root=repository_root,
        created_at=now - timedelta(minutes=5),
        oos_artifact_hash=BAD_ARTIFACT_HASH,
    )
    request_id, available_at = _seed_parent_and_prospective(
        factory,
        repository_root=repository_root,
        decision_time=now - timedelta(minutes=1),
    )
    as_of = available_at + timedelta(seconds=1)
    service = TrustedProspectiveShadowOperations(
        factory,
        prospective_config=load_prospective_candidate_config(
            repository_root / "config"
        ),
        clock=lambda: as_of,
    )
    with pytest.raises(
        ProspectiveShadowOperationError,
        match="PROSPECTIVE_SHADOW_SOURCE_BINDING_MISMATCH",
    ):
        service.prepare(
            run_id=initialized.spec.run_id,
            prospective_request_id=request_id,
            as_of=as_of,
        )
