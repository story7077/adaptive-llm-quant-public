from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading.domain.hashing import canonical_hash, canonical_json
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
    CandidateEvaluationDatasetV1,
    CandidateEvaluationError,
    CandidateOutcomeV1,
    build_candidate_evaluation_dataset,
    build_candidate_evaluation_scenario,
    evaluate_candidate_twice,
)
from trading.research.candidate_process import (
    CandidateExecutionSecurityV1,
    CandidateProcessLimitsV1,
    CandidateProcessResultV1,
    IsolatedCandidateExecutor,
    build_candidate_execution_security,
    build_candidate_process_result,
)
from trading.research.contracts import OosVerdict, OosWorkerRequestV1
from trading.research.evaluation_contracts import (
    FalsificationEvaluationContractV1,
    KnownFactorReturnV1,
)
from trading.research.oos_dataset_producer import (
    PrivateOosDatasetProducerError,
    produce_private_oos_dataset,
)
from trading.research.oos_worker import evaluate_private_request

NOW = datetime(2026, 1, 9, 15, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _feature(
    name: str,
    value: float,
    *,
    available_at: datetime = NOW - timedelta(minutes=1),
) -> CandidateFeatureValueV1:
    return CandidateFeatureValueV1(
        name=name,
        value=value,
        source_event_time=available_at - timedelta(seconds=1),
        available_at=available_at,
        source_revision=0,
        revision_available_at=available_at,
        revision_was_known_at_cutoff=True,
        source_hash=canonical_hash({"name": name, "value": value}),
    )


def _instrument(symbol: str, value: float) -> CandidateInstrumentInputV1:
    return CandidateInstrumentInputV1(
        symbol=symbol,
        current_weight=0,
        membership_available_at=NOW - timedelta(days=30),
        membership_valid_from=NOW - timedelta(days=365),
        membership_valid_until=None,
        instrument_is_non_survivor=False,
        features=(_feature("signal", value),),
    )


def _request(
    *,
    artifact_hash: str = HASH_A,
    instruments: tuple[CandidateInstrumentInputV1, ...] | None = None,
) -> CandidateDecisionRequestV1:
    universe = instruments or (_instrument("QQQ", 1), _instrument("SOXX", 0.5))
    caps = {
        item.symbol: 0.8 if item.symbol == "QQQ" else 0.45
        for item in universe
    }
    constraints = CandidateDecisionConstraintsV1(
        maximum_gross_weight=1,
        minimum_cash_weight=0,
        maximum_weight_by_symbol=dict(sorted(caps.items())),
        numeric_tolerance=1e-12,
    )
    return build_candidate_decision_request(
        request_id="candidate-request-001",
        challenger_id="challenger-001",
        candidate_artifact_hash=artifact_hash,
        strategy_id="T1",
        strategy_version="1.1.0",
        decision_time=NOW,
        signal_data_cutoff=NOW,
        variant=CandidateEvaluationVariantV1(),
        instruments=universe,
        constraints=constraints,
        strategy_parameters={"signal_scale": 1.0},
        source_data_manifest_hash=HASH_B,
    )


def _outcome(
    symbol: str,
    *,
    forward_return: float,
    baseline_target_weight: float,
    baseline_current_weight: float = 0,
) -> CandidateOutcomeV1:
    return CandidateOutcomeV1(
        symbol=symbol,
        trade_id=f"trade-{symbol.lower()}",
        forward_return=forward_return,
        baseline_current_weight=baseline_current_weight,
        baseline_target_weight=baseline_target_weight,
        commission_bps=1,
        spread_bps=4,
        delay_bps=2,
        adv_usd=1_000_000,
        market_return=0.01,
        sector_return=0.012,
        known_factor_returns=(
            KnownFactorReturnV1(factor_id="momentum", return_value=0.004),
        ),
        regime="UP",
        outcome_available_at=NOW + timedelta(days=1),
    )


def _contract() -> FalsificationEvaluationContractV1:
    return FalsificationEvaluationContractV1(
        contract_version="candidate-abi-test-v1",
        minimum_observation_count=2,
        minimum_session_count=1,
        maximum_source_age_seconds=31_536_000,
        minimum_universe_coverage_ratio=1,
        minimum_non_survivor_coverage_ratio=1,
        minimum_variant_session_coverage_ratio=1,
        minimum_base_mean_net_return=-1,
        maximum_parameter_relative_deviation=1,
        minimum_neighborhood_edge_ratio=0,
        minimum_neighborhood_pass_fraction=0,
        maximum_placebo_edge_ratio=1,
        maximum_single_symbol_positive_edge_share=1,
        maximum_single_month_positive_edge_share=1,
        top_trade_count=1,
        minimum_top_trades_removed_edge_ratio=0,
        cost_stress_multipliers=(1, 2, 3),
        minimum_cost_stress_mean_net_return=-1,
        delay_stress_multiplier=2,
        minimum_delay_stress_mean_net_return=-1,
        spread_stress_multiplier=2,
        minimum_spread_stress_mean_net_return=-1,
        basis_points_per_unit_return=10_000,
        maximum_adv_participation_ratio=1,
        minimum_capacity_pass_fraction=0,
        minimum_market_neutral_edge_ratio=-1,
        minimum_sector_neutral_edge_ratio=-1,
        minimum_known_factor_neutral_edge_ratio=-1,
        regression_variance_epsilon=1e-12,
        minimum_regime_observations=1,
        minimum_regime_pass_fraction=0,
        minimum_regime_mean_net_return=-1,
        minimum_ablation_edge_ratio=0,
        minimum_ablation_pass_fraction=0,
        numeric_tolerance=1e-12,
    )


class _FixedExecutor:
    def execute(
        self,
        request: CandidateDecisionRequestV1,
    ) -> CandidateDecisionResponseV1:
        return build_candidate_decision_response(
            request=request,
            targets=(
                CandidateTargetV1(symbol="QQQ", score=1, target_weight=0.6),
                CandidateTargetV1(symbol="SOXX", score=0.5, target_weight=0.3),
            ),
            diagnostics={"candidate_owned_performance": False},
        )


class _NondeterministicExecutor:
    calls = 0

    def execute(
        self,
        request: CandidateDecisionRequestV1,
    ) -> CandidateDecisionResponseV1:
        self.calls += 1
        qqq = 0.6 if self.calls <= 1 else 0.5
        return build_candidate_decision_response(
            request=request,
            targets=(
                CandidateTargetV1(symbol="QQQ", score=1, target_weight=qqq),
                CandidateTargetV1(symbol="SOXX", score=0.5, target_weight=0.3),
            ),
        )


class _FailingExecutor:
    def execute(
        self,
        request: CandidateDecisionRequestV1,
    ) -> CandidateDecisionResponseV1:
        raise RuntimeError(request.request_id)


class _ProcessTransport:
    def __init__(
        self,
        *,
        stdout_factory: object,
        exit_code: int | None = 0,
        timed_out: bool = False,
        resource_limit_exceeded: bool = False,
        artifact_hash: str = HASH_A,
    ) -> None:
        self._stdout_factory = stdout_factory
        self._exit_code = exit_code
        self._timed_out = timed_out
        self._resource_limit_exceeded = resource_limit_exceeded
        self._artifact_hash = artifact_hash

    def invoke(
        self,
        *,
        request_json: str,
        request_hash: str,
        security: CandidateExecutionSecurityV1,
    ) -> CandidateProcessResultV1:
        del request_json
        if callable(self._stdout_factory):
            stdout = self._stdout_factory(request_hash)
        else:
            stdout = self._stdout_factory
        assert isinstance(stdout, str)
        return build_candidate_process_result(
            invocation_id="candidate-process-001",
            request_hash=request_hash,
            candidate_artifact_hash=self._artifact_hash,
            security_contract_hash=security.security_contract_hash,
            exit_code=self._exit_code,
            timed_out=self._timed_out,
            resource_limit_exceeded=self._resource_limit_exceeded,
            stdout_utf8=stdout,
            stderr_sha256=hashlib.sha256(b"").hexdigest(),
            stderr_bytes=0,
        )


def _dataset() -> tuple[CandidateDecisionRequestV1, CandidateEvaluationDatasetV1]:
    request = _request()
    scenario = build_candidate_evaluation_scenario(
        scenario_id="scenario-001",
        request=request,
        outcomes=(
            _outcome("QQQ", forward_return=0.02, baseline_target_weight=0.5),
            _outcome("SOXX", forward_return=0.03, baseline_target_weight=0.25),
        ),
        evaluation_nav_usd=100_000,
    )
    dataset = build_candidate_evaluation_dataset(
        dataset_id="candidate-dataset-001",
        challenger_id=request.challenger_id,
        candidate_artifact_hash=request.candidate_artifact_hash,
        source_data_manifest_hash=request.source_data_manifest_hash,
        eligible_instrument_count=2,
        eligible_non_survivor_count=0,
        scenarios=(scenario,),
    )
    return request, dataset


def _security(
    *,
    maximum_stdout_bytes: int = 32_768,
) -> CandidateExecutionSecurityV1:
    return build_candidate_execution_security(
        isolation_kind="CODEX_WINDOWS_RESTRICTED_TOKEN",
        isolation_version="1.0.0",
        candidate_artifact_hash=HASH_A,
        candidate_tree_hash=HASH_B,
        runtime_executable_hash=HASH_C,
        worker_code_hash=HASH_D,
        declared_entrypoint="trading.strategies.challengers.example:decide",
        limits=CandidateProcessLimitsV1(
            timeout_seconds=10,
            maximum_stdout_bytes=maximum_stdout_bytes,
            maximum_stderr_bytes=8_192,
            maximum_memory_bytes=268_435_456,
            maximum_processes=4,
        ),
    )


def _response_json(request: CandidateDecisionRequestV1) -> str:
    return canonical_json(
        build_candidate_decision_response(
            request=request,
            targets=(
                CandidateTargetV1(symbol="QQQ", score=1, target_weight=0.6),
                CandidateTargetV1(symbol="SOXX", score=0.5, target_weight=0.3),
            ),
        )
    )


def test_candidate_request_contains_only_point_in_time_features() -> None:
    request = _request()
    payload = request.model_dump(mode="json")
    serialized = str(payload)

    assert "forward_return" not in serialized
    assert "baseline_target_weight" not in serialized
    assert "outcome_available_at" not in serialized
    assert request.signal_data_cutoff == NOW


def test_candidate_request_rejects_future_available_feature() -> None:
    future_input = CandidateInstrumentInputV1(
        symbol="QQQ",
        current_weight=0,
        membership_available_at=NOW - timedelta(days=1),
        membership_valid_from=NOW - timedelta(days=365),
        membership_valid_until=None,
        instrument_is_non_survivor=False,
        features=(
            _feature(
                "signal",
                1,
                available_at=NOW + timedelta(microseconds=1),
            ),
        ),
    )

    with pytest.raises(ValidationError, match="future or revised data"):
        _request(instruments=(future_input,))


def test_candidate_response_rejects_short_new_symbol_and_overexposure() -> None:
    request = _request()

    with pytest.raises(ValidationError):
        CandidateTargetV1(symbol="QQQ", score=1, target_weight=-0.01)

    with pytest.raises(ValueError, match="introduced or omitted"):
        build_candidate_decision_response(
            request=request,
            targets=(
                CandidateTargetV1(symbol="NVDA", score=1, target_weight=0.2),
                CandidateTargetV1(symbol="QQQ", score=1, target_weight=0.6),
            ),
        )

    with pytest.raises(ValueError, match="symbol cap"):
        build_candidate_decision_response(
            request=request,
            targets=(
                CandidateTargetV1(symbol="QQQ", score=1, target_weight=0.81),
                CandidateTargetV1(symbol="SOXX", score=1, target_weight=0.19),
            ),
        )


def test_candidate_response_rejects_artifact_binding_mismatch() -> None:
    request = _request()
    payload = {
        "schema_version": "candidate_decision_response_v1",
        "request_id": request.request_id,
        "request_hash": request.request_hash,
        "challenger_id": request.challenger_id,
        "candidate_artifact_hash": HASH_C,
        "targets": (
            CandidateTargetV1(symbol="QQQ", score=1, target_weight=0.5),
            CandidateTargetV1(symbol="SOXX", score=1, target_weight=0.2),
        ),
        "diagnostics": {},
    }
    response = CandidateDecisionResponseV1.model_validate(
        {**payload, "output_hash": canonical_hash(payload)}
    )

    with pytest.raises(ValueError, match="binding mismatch"):
        response.assert_bound_to(request)


def test_host_calculates_returns_costs_capacity_and_replay() -> None:
    _, dataset = _dataset()
    result = evaluate_candidate_twice(
        dataset=dataset,
        executor=_FixedExecutor(),
        evaluation_contract=_contract(),
        trace_id="candidate-trace-001",
        config_hash=HASH_C,
        code_hash=HASH_D,
        created_at=NOW + timedelta(days=2),
    )

    assert result.replay.deterministic_match is True
    qqq, soxx = result.trace.observations
    assert qqq.instrument_id == "QQQ"
    assert qqq.candidate_return == pytest.approx(0.012)
    assert qqq.baseline_return == pytest.approx(0.01)
    assert qqq.modeled_cost == pytest.approx(0.6 * 5 / 10_000)
    assert qqq.capacity_used_usd == pytest.approx(60_000)
    assert soxx.candidate_return == pytest.approx(0.009)
    assert result.trace.candidate_artifact_hash == HASH_A


def test_nondeterministic_or_failed_candidate_fails_closed() -> None:
    _, dataset = _dataset()
    common = {
        "dataset": dataset,
        "evaluation_contract": _contract(),
        "trace_id": "candidate-trace-001",
        "config_hash": HASH_C,
        "code_hash": HASH_D,
        "created_at": NOW + timedelta(days=2),
    }

    with pytest.raises(CandidateEvaluationError, match="not deterministic"):
        evaluate_candidate_twice(
            executor=_NondeterministicExecutor(),
            **common,
        )
    with pytest.raises(CandidateEvaluationError, match="execution rejected"):
        evaluate_candidate_twice(
            executor=_FailingExecutor(),
            **common,
        )


def test_attested_process_executor_accepts_only_bound_single_json() -> None:
    request = _request()
    executor = IsolatedCandidateExecutor(
        security=_security(),
        transport=_ProcessTransport(
            stdout_factory=lambda _: _response_json(request),
        ),
    )

    response = executor.execute(request)

    assert response.request_hash == request.request_hash
    assert response.targets[0].symbol == "QQQ"


@pytest.mark.parametrize(
    ("transport", "message"),
    [
        (_ProcessTransport(stdout_factory="{broken"), "not one valid JSON"),
        (
            _ProcessTransport(stdout_factory="{}", exit_code=2),
            "exited unsuccessfully",
        ),
        (
            _ProcessTransport(stdout_factory="{}", timed_out=True),
            "timed out",
        ),
        (
            _ProcessTransport(stdout_factory="{}", resource_limit_exceeded=True),
            "resource limit",
        ),
        (
            _ProcessTransport(stdout_factory="{}", artifact_hash=HASH_B),
            "binding mismatch",
        ),
    ],
)
def test_attested_process_executor_fails_closed(
    transport: _ProcessTransport,
    message: str,
) -> None:
    executor = IsolatedCandidateExecutor(
        security=_security(),
        transport=transport,
    )

    with pytest.raises(ValueError, match=message):
        executor.execute(_request())


def test_attested_process_executor_enforces_output_limit() -> None:
    executor = IsolatedCandidateExecutor(
        security=_security(maximum_stdout_bytes=1),
        transport=_ProcessTransport(stdout_factory="{}"),
    )

    with pytest.raises(ValueError, match="stdout exceeded"):
        executor.execute(_request())


def test_security_contract_cannot_enable_network_credentials_or_routing() -> None:
    security = _security()
    payload = security.model_dump(mode="python")
    payload["network_access_permitted"] = True

    with pytest.raises(ValidationError):
        CandidateExecutionSecurityV1.model_validate(payload)


def test_trusted_producer_feeds_lockbox_without_returning_private_rows(
    tmp_path: Path,
) -> None:
    _, dataset = _dataset()
    private_root = tmp_path / "private"
    private_root.mkdir()
    manifest = produce_private_oos_dataset(
        dataset=dataset,
        executor=_FixedExecutor(),
        evaluation_contract_hash=HASH_C,
        config_hash=HASH_C,
        code_hash=HASH_D,
        created_at=NOW + timedelta(days=2),
        private_root=private_root,
        private_dataset_id="locked-candidate-001",
    )
    request_payload = {
        "schema_version": "oos_worker_request_v1",
        "request_id": "oos-candidate-request-001",
        "challenger_id": dataset.challenger_id,
        "experiment_family": "candidate-abi-family",
        "submission_number": 1,
        "candidate_artifact_hash": dataset.candidate_artifact_hash,
        "evaluation_contract_hash": HASH_C,
        "reservation_id": "oos-reservation-001",
        "reservation_hash": HASH_D,
        "oos_budget_ordinal": 1,
        "dataset_id": manifest.dataset_id,
        "dataset_manifest_hash": manifest.dataset_hash,
        "expected_source_data_manifest_hash": (
            manifest.source_data_manifest_hash
        ),
        "expected_candidate_replay_hash": manifest.candidate_replay_hash,
        "expected_trusted_producer_version": (
            manifest.trusted_producer_version
        ),
        "data_available_cutoff": NOW + timedelta(days=2),
        "minimum_common_sessions": 126,
        "minimum_mean_daily_difference": -1.0,
        "annualization_sessions": 252,
        "newey_west_lag": 5,
        "bootstrap_seed": 7077,
        "bootstrap_block_length": 10,
        "bootstrap_samples": 100,
        "base_cost_bps": 10,
        "cost_sensitivity_bps": (0, 5, 10),
        "evaluated_at": NOW + timedelta(days=3),
        "expires_at": NOW + timedelta(days=3, minutes=15),
    }
    worker_request = OosWorkerRequestV1.model_validate(
        {
            **request_payload,
            "request_hash": canonical_hash(request_payload),
        }
    )

    response = evaluate_private_request(worker_request, private_root=private_root)

    assert response.result.verdict is OosVerdict.FAIL
    assert response.result.reason_codes == ["INSUFFICIENT_COMMON_SESSIONS"]
    assert manifest.common_session_count == 1
    serialized_manifest = manifest.model_dump_json()
    assert "candidate_return" not in serialized_manifest
    assert "matched_baseline_return" not in serialized_manifest
    with pytest.raises(PrivateOosDatasetProducerError, match="append-only"):
        produce_private_oos_dataset(
            dataset=dataset,
            executor=_FixedExecutor(),
            evaluation_contract_hash=HASH_C,
            config_hash=HASH_C,
            code_hash=HASH_D,
            created_at=NOW + timedelta(days=2),
            private_root=private_root,
            private_dataset_id="locked-candidate-001",
        )
    assert not tuple(private_root.glob(".oos-dataset-*.tmp"))


def test_private_oos_producer_rejects_path_traversal_before_execution(
    tmp_path: Path,
) -> None:
    _, dataset = _dataset()
    private_root = tmp_path / "private"
    private_root.mkdir()

    with pytest.raises(PrivateOosDatasetProducerError, match="ID is invalid"):
        produce_private_oos_dataset(
            dataset=dataset,
            executor=_FailingExecutor(),
            evaluation_contract_hash=HASH_C,
            config_hash=HASH_C,
            code_hash=HASH_D,
            created_at=NOW + timedelta(days=2),
            private_root=private_root,
            private_dataset_id="../escape",
        )

    assert not (tmp_path / "escape.json").exists()
