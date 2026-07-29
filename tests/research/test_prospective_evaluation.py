from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading.data.q1_pit import AlignedDailyInputs, CompletedDailySeries
from trading.domain.hashing import canonical_hash
from trading.research.candidate_abi import (
    CandidateDecisionRequestV1,
    CandidateDecisionResponseV1,
    CandidateEvaluationVariantV1,
    CandidateFeatureValueV1,
    CandidateInstrumentInputV1,
    build_candidate_decision_request,
)
from trading.research.evaluation_contracts import KnownFactorReturnV1
from trading.research.prospective import (
    ProspectiveExecutionEvidenceV1,
    ProspectiveExecutionStatus,
    ProspectiveRequestEvidenceV1,
    ProspectiveSourceBarV1,
    ProspectiveSourceManifestV1,
    build_candidate_price_features,
    load_prospective_candidate_config,
)
from trading.research.prospective_evaluation import (
    ProspectiveEvaluationConfigBundle,
    ProspectiveEvaluationError,
    ProspectiveEvaluationRecord,
    build_prospective_evaluation_dataset,
    load_prospective_evaluation_config,
)
from trading.research.prospective_outcomes import (
    ProspectiveOutcomeSourceBarV1,
    build_prospective_outcome_evidence,
    build_prospective_outcome_failure,
    load_prospective_outcome_config,
)
from trading.strategies.challengers.q1_det_v2_0_0.decision import (
    decide,
)

ARTIFACT_HASH = "a" * 64
CONFIG_HASH = "b" * 64
SYMBOLS = ("GLD", "QQQ", "SGOV", "SOXX", "TLT")
BASE_DECISION = datetime(2027, 1, 4, 15, 0, tzinfo=UTC)


class _InProcessCandidate:
    def execute(
        self,
        request: CandidateDecisionRequestV1,
    ) -> CandidateDecisionResponseV1:
        return CandidateDecisionResponseV1.model_validate(
            decide(request.model_dump(mode="json"))
        )


def _small_config(
    config_dir: Path,
    *,
    required_sessions: int = 3,
) -> ProspectiveEvaluationConfigBundle:
    loaded = load_prospective_evaluation_config(config_dir)
    selection = loaded.config.source_selection.model_copy(
        update={"required_common_sessions": required_sessions}
    )
    config = loaded.config.model_copy(
        update={"source_selection": selection}
    )
    return replace(
        loaded,
        config=config,
        manifest_hash=canonical_hash(
            {
                "test_config": config,
                "prospective": loaded.prospective_manifest_hash,
                "outcomes": loaded.outcome_manifest_hash,
            }
        ),
    )


def _market_inputs() -> AlignedDailyInputs:
    first = date(2026, 1, 1)
    sessions = tuple(first + timedelta(days=index) for index in range(220))
    cycle = (
        Decimal("0.00"),
        Decimal("0.45"),
        Decimal("-0.20"),
        Decimal("0.35"),
        Decimal("-0.15"),
        Decimal("0.25"),
    )
    series: dict[str, CompletedDailySeries] = {}
    for symbol_index, symbol in enumerate(SYMBOLS):
        cycle_scale = Decimal("1") + Decimal(symbol_index) / Decimal("20")
        closes = tuple(
            Decimal("100")
            + Decimal(symbol_index)
            + Decimal(index) * Decimal("0.08")
            + cycle[index % len(cycle)] * cycle_scale
            for index in range(220)
        )
        events = tuple(
            datetime.combine(
                session,
                datetime.min.time(),
                tzinfo=UTC,
            )
            + timedelta(hours=21)
            for session in sessions
        )
        available = tuple(
            item + timedelta(minutes=5) for item in events
        )
        series[symbol] = CompletedDailySeries(
            symbol=symbol,
            session_dates=sessions,
            adjusted_closes=closes,
            volumes=tuple(Decimal("1000000") for _ in sessions),
            bar_ids=tuple(
                f"pit-{symbol.lower()}-{index:03d}"
                for index in range(220)
            ),
            event_times=events,
            available_ats=available,
            payload_hashes=tuple(
                canonical_hash(
                    {"symbol": symbol, "session": session.isoformat()}
                )
                for session in sessions
            ),
        )
    return AlignedDailyInputs(
        session_dates=sessions,
        series=series,
        source_bar_ids=tuple(
            sorted(
                bar_id
                for item in series.values()
                for bar_id in item.bar_ids
            )
        ),
        signal_data_cutoff=BASE_DECISION,
    )


def _source_bars(
    market: AlignedDailyInputs,
) -> tuple[ProspectiveSourceBarV1, ...]:
    return tuple(
        ProspectiveSourceBarV1(
            bar_id=series.bar_ids[index],
            symbol=symbol,
            session_date=session,
            source_event_time=series.event_times[index],
            available_at=series.available_ats[index],
            payload_hash=series.payload_hashes[index],
        )
        for symbol, series in sorted(market.series.items())
        for index, session in enumerate(series.session_dates)
    )


def _feature(
    *,
    name: str,
    value: float,
    decision_time: datetime,
    symbol: str,
) -> CandidateFeatureValueV1:
    available = decision_time - timedelta(hours=1)
    return CandidateFeatureValueV1(
        name=name,
        value=value,
        source_event_time=available - timedelta(minutes=1),
        available_at=available,
        source_revision=0,
        revision_available_at=available,
        revision_was_known_at_cutoff=True,
        source_hash=canonical_hash(
            {"symbol": symbol, "name": name, "value": value}
        ),
    )


def _instruments(
    *,
    market: AlignedDailyInputs,
    decision_time: datetime,
    current_weights: dict[str, float],
) -> tuple[CandidateInstrumentInputV1, ...]:
    built: list[CandidateInstrumentInputV1] = []
    for symbol in SYMBOLS:
        features = list(
            build_candidate_price_features(
                series=market.series[symbol],
                qqq_series=market.series["QQQ"],
                short_return_sessions=63,
                long_return_sessions=126,
                moving_average_sessions=200,
                realized_volatility_sessions=63,
                downside_beta_sessions=126,
                annualization_sessions=252,
                realized_volatility_ddof=1,
                minimum_variance=1e-12,
                source_revision=0,
                formula_version="test-prospective-v2",
            )
        )
        features.extend(
            (
                _feature(
                    name="parent_target_weight",
                    value=(
                        0.50
                        if symbol == "QQQ"
                        else 0.20 if symbol == "SOXX" else 0.0
                    ),
                    decision_time=decision_time,
                    symbol=symbol,
                ),
                _feature(
                    name="parent_score",
                    value=0.5,
                    decision_time=decision_time,
                    symbol=symbol,
                ),
                _feature(
                    name="completed_sessions_since_review",
                    value=21.0,
                    decision_time=decision_time,
                    symbol=symbol,
                ),
            )
        )
        built.append(
            CandidateInstrumentInputV1(
                symbol=symbol,
                current_weight=current_weights[symbol],
                membership_available_at=(
                    decision_time - timedelta(days=365)
                ),
                membership_valid_from=(
                    decision_time - timedelta(days=365)
                ),
                membership_valid_until=None,
                instrument_is_non_survivor=False,
                features=tuple(
                    sorted(features, key=lambda item: item.name)
                ),
            )
        )
    return tuple(built)


def _outcome_bars(
    *,
    decision_time: datetime,
) -> tuple[ProspectiveOutcomeSourceBarV1, ...]:
    symbols = tuple(sorted({*SYMBOLS, "SPY", "HYG", "IWM"}))
    implementation = decision_time + timedelta(days=1)
    evaluation = decision_time + timedelta(days=2)
    rows: list[ProspectiveOutcomeSourceBarV1] = []
    for session_index, event_time in enumerate(
        (implementation, evaluation)
    ):
        for symbol in symbols:
            close = 100.0 if session_index == 0 else 101.0
            rows.append(
                ProspectiveOutcomeSourceBarV1(
                    bar_id=(
                        f"outcome-{decision_time.date()}-"
                        f"{session_index}-{symbol.lower()}"
                    ),
                    symbol=symbol,
                    session_date=event_time.date(),
                    source_event_time=event_time,
                    available_at=event_time + timedelta(minutes=15),
                    adjusted_close=close,
                    volume=1_000_000,
                    payload_hash=canonical_hash(
                        {
                            "symbol": symbol,
                            "session": session_index,
                            "decision": decision_time,
                        }
                    ),
                )
            )
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                item.session_date,
                item.symbol,
                item.bar_id,
            ),
        )
    )


def _records(
    config_dir: Path,
    *,
    count: int,
) -> tuple[ProspectiveEvaluationRecord, ...]:
    evaluation_config = load_prospective_evaluation_config(config_dir)
    prospective = evaluation_config
    outcomes = load_prospective_outcome_config(config_dir)
    prospective_candidate = load_prospective_candidate_config(config_dir)
    market_template = _market_inputs()
    source_bars = _source_bars(market_template)
    executor = _InProcessCandidate()
    records: list[ProspectiveEvaluationRecord] = []
    current = {symbol: 0.0 for symbol in SYMBOLS}
    prior_request_id: str | None = None
    prior_execution_hash: str | None = None
    for index in range(count):
        decision_time = BASE_DECISION + timedelta(days=index)
        market = replace(
            market_template,
            signal_data_cutoff=decision_time,
        )
        source_payload = {
            "schema_version": (
                "candidate_prospective_source_manifest_v1"
            ),
            "producer_version": "test-prospective-v2",
            "challenger_id": "challenger-prospective-evaluation",
            "candidate_artifact_hash": ARTIFACT_HASH,
            "parent_run_id": "parent-run-prospective",
            "parent_portfolio_decision_id": (
                f"parent-decision-{index:03d}"
            ),
            "parent_decision_hash": canonical_hash(
                {"parent": index}
            ),
            "parent_input_manifest_hash": canonical_hash(
                {"input": index}
            ),
            "parent_scheduled_at": decision_time - timedelta(seconds=5),
            "evaluation_anchor_id": "anchor-prospective",
            "evaluation_anchor_hash": "c" * 64,
            "prior_prospective_request_id": prior_request_id,
            "prior_execution_hash": prior_execution_hash,
            "state_source": (
                "CASH_ONLY_AT_EVALUATION_ANCHOR"
                if prior_request_id is None
                else "PRIOR_VERIFIED_TARGETS"
            ),
            "market_dataset_version": (
                "alpaca_iex_adjusted_all_v1"
            ),
            "signal_data_cutoff": decision_time,
            "completed_session_dates": market.session_dates,
            "source_bars": source_bars,
            "formula_contract_hash": "d" * 64,
            "host_config_manifest_hash": (
                prospective.prospective_manifest_hash
            ),
        }
        source = ProspectiveSourceManifestV1.model_validate(
            {
                **source_payload,
                "manifest_hash": canonical_hash(source_payload),
            }
        )
        request = build_candidate_decision_request(
            request_id=f"prospective-evaluation-request-{index:03d}",
            challenger_id="challenger-prospective-evaluation",
            candidate_artifact_hash=ARTIFACT_HASH,
            strategy_id="Q1-DET",
            strategy_version="2.0.0",
            decision_time=decision_time,
            signal_data_cutoff=decision_time,
            variant=CandidateEvaluationVariantV1(),
            instruments=_instruments(
                market=market,
                decision_time=decision_time,
                current_weights=current,
            ),
            constraints=prospective_candidate.config.constraints,
            strategy_parameters=prospective.strategy_parameters,
            source_data_manifest_hash=source.manifest_hash,
        )
        request_payload = {
            "schema_version": (
                "candidate_prospective_request_evidence_v1"
            ),
            "prospective_request_id": request.request_id,
            "challenger_id": request.challenger_id,
            "candidate_artifact_bundle_id": (
                "candidate-bundle-prospective-evaluation"
            ),
            "candidate_artifact_hash": ARTIFACT_HASH,
            "candidate_config_hash": CONFIG_HASH,
            "strategy_config_content_sha256": "e" * 64,
            "parent_run_id": "parent-run-prospective",
            "parent_portfolio_decision_id": (
                f"parent-decision-{index:03d}"
            ),
            "parent_scheduled_at": decision_time - timedelta(seconds=5),
            "calendar_session_id": f"calendar-{index:03d}",
            "evaluation_anchor_id": "anchor-prospective",
            "prior_prospective_request_id": prior_request_id,
            "source_manifest": source,
            "request": request,
            "created_at": decision_time,
            "real_order_routing": False,
            "automatic_promotion_enabled": False,
            "challenger_lifecycle_advance_enabled": False,
            "shadow_activation_enabled": False,
        }
        request_evidence = ProspectiveRequestEvidenceV1.model_validate(
            {
                **request_payload,
                "evidence_hash": canonical_hash(request_payload),
            }
        )
        response = executor.execute(request)
        execution_payload = {
            "schema_version": (
                "candidate_prospective_execution_evidence_v1"
            ),
            "execution_id": f"prospective-execution-{index:03d}",
            "prospective_request_id": request.request_id,
            "challenger_id": request.challenger_id,
            "candidate_artifact_hash": ARTIFACT_HASH,
            "request_hash": request.request_hash,
            "status": ProspectiveExecutionStatus.SUCCEEDED,
            "runtime_attestation_hash": "f" * 64,
            "security_contract_hash": "1" * 64,
            "primary_response": response,
            "replay_response": response,
            "deterministic_match": True,
            "error_code": None,
            "created_at": decision_time,
            "real_order_routing": False,
            "evidence_recorded": True,
            "challenger_status_advanced": False,
            "shadow_started": False,
        }
        execution = ProspectiveExecutionEvidenceV1.model_validate(
            {
                **execution_payload,
                "execution_hash": canonical_hash(execution_payload),
            }
        )
        outcome_bars = _outcome_bars(decision_time=decision_time)
        forward_return = 101.0 / 100.0 - 1.0
        target = {
            item.symbol: item.target_weight
            for item in response.targets
        }
        outcome = build_prospective_outcome_evidence(
            request=request_evidence,
            execution=execution,
            config=outcomes,
            implementation_calendar_session_id=(
                f"calendar-implementation-{index:03d}"
            ),
            evaluation_calendar_session_id=(
                f"calendar-evaluation-{index:03d}"
            ),
            implementation_close_at=decision_time + timedelta(days=1),
            evaluation_close_at=decision_time + timedelta(days=2),
            outcome_data_cutoff=(
                decision_time + timedelta(days=2, hours=2)
            ),
            evaluation_nav_usd=100_000,
            candidate_current_weights=current,
            candidate_target_weights=target,
            baseline_current_weights=current,
            baseline_target_weights=target,
            forward_returns={
                symbol: forward_return for symbol in SYMBOLS
            },
            adv_usd={symbol: 10_000_000 for symbol in SYMBOLS},
            market_return=forward_return,
            sector_return=forward_return,
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
            source_bars=outcome_bars,
            created_at=decision_time + timedelta(days=2, hours=2),
        )
        records.append(
            ProspectiveEvaluationRecord(
                request=request_evidence,
                execution=execution,
                outcome=outcome,
                market_inputs=market,
                decision_session_ordinal=1_000 + index,
                calendar_path_hash=canonical_hash(
                    {"calendar_path": tuple(range(index + 1))}
                ),
            )
        )
        current = target
        prior_request_id = request.request_id
        prior_execution_hash = execution.execution_hash
    return tuple(records)


def test_forward_dataset_is_stateful_deterministic_and_future_stable(
    repository_root: Path,
) -> None:
    config = _small_config(repository_root / "config")
    records = _records(repository_root / "config", count=4)
    executor = _InProcessCandidate()

    first = build_prospective_evaluation_dataset(
        config_bundle=config,
        records=records[:3],
        terminal_failures=(),
        state_executor=executor,
    )
    with_future_record = build_prospective_evaluation_dataset(
        config_bundle=config,
        records=records,
        terminal_failures=(),
        state_executor=executor,
    )

    assert first.dataset.dataset_hash == with_future_record.dataset.dataset_hash
    assert first.selected_request_count == 3
    assert first.base_scenario_count == 3
    assert first.variant_scenario_count > first.base_scenario_count
    assert set(first.variant_coverage.values()) == {1.0}
    assert len(
        {
            item.base_source_manifest_hash
            for item in first.dataset.source_manifest.bindings
            if item.scenario_id == item.base_scenario_id
        }
    ) == 3
    variant = next(
        item
        for item in first.dataset.scenarios
        if item.request.variant.parameter_neighborhood_id
        == "SLEEVE_CAP_0_25"
        and item.request.decision_time
        == records[1].request.request.decision_time
    )
    previous = next(
        item
        for item in first.dataset.scenarios
        if item.request.variant.parameter_neighborhood_id
        == "SLEEVE_CAP_0_25"
        and item.request.decision_time
        == records[0].request.request.decision_time
    )
    previous_response = executor.execute(previous.request)
    assert {
        item.symbol: item.current_weight
        for item in variant.request.instruments
    } == {
        item.symbol: item.target_weight
        for item in previous_response.targets
    }
    serialized_requests = str(
        [
            item.request.model_dump(mode="json")
            for item in first.dataset.scenarios
        ]
    )
    assert "forward_return" not in serialized_requests
    assert "outcome_available_at" not in serialized_requests


def test_terminal_failure_coverage_fails_closed(
    repository_root: Path,
) -> None:
    config_dir = repository_root / "config"
    config = _small_config(config_dir)
    records = _records(config_dir, count=4)
    outcomes = load_prospective_outcome_config(config_dir)
    failure = build_prospective_outcome_failure(
        request=records[3].request,
        execution=records[3].execution,
        config=outcomes,
        implementation_calendar_session_id="calendar-failure-impl",
        evaluation_calendar_session_id="calendar-failure-eval",
        outcome_data_cutoff=BASE_DECISION + timedelta(days=10),
    )

    with pytest.raises(
        ProspectiveEvaluationError,
        match="REQUEST_COVERAGE_INSUFFICIENT",
    ):
        build_prospective_evaluation_dataset(
            config_bundle=config,
            records=records[:3],
            terminal_failures=(failure,),
            state_executor=_InProcessCandidate(),
        )
