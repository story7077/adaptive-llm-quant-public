from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from trading.domain.hashing import canonical_hash
from trading.research.contracts import FalsificationStatus
from trading.research.evaluation_contracts import (
    BASE_VARIANT_ID,
    CandidateEvaluationObservationV1,
    CandidateEvaluationTraceV1,
    FalsificationEvaluationContractV1,
    KnownFactorReturnV1,
    build_candidate_evaluation_trace,
)
from trading.research.evaluators import (
    TRACE_EVALUATOR_TEST_IDS,
    build_trusted_evaluator_factory,
)
from trading.research.falsification import (
    MANDATORY_FALSIFICATION_TESTS,
    ExperimentBudget,
)
from trading.research.falsification_runner import (
    AutomatedFalsificationRunner,
    FalsificationRunContext,
)

ARTIFACT_HASH = "a" * 64
DATA_HASH = "b" * 64
REPLAY_HASH = "c" * 64
VARIANT_FIELDS = (
    "parameter_neighborhood_id",
    "data_ablation_id",
    "date_shift_id",
    "inversion_id",
    "shuffle_id",
)


def _contract() -> FalsificationEvaluationContractV1:
    return FalsificationEvaluationContractV1(
        contract_version="trace-contract-v1",
        minimum_observation_count=20,
        minimum_session_count=10,
        maximum_source_age_seconds=7200,
        minimum_universe_coverage_ratio=1.0,
        minimum_non_survivor_coverage_ratio=1.0,
        minimum_variant_session_coverage_ratio=1.0,
        minimum_base_mean_net_return=0.001,
        maximum_parameter_relative_deviation=0.25,
        minimum_neighborhood_edge_ratio=0.7,
        minimum_neighborhood_pass_fraction=1.0,
        maximum_placebo_edge_ratio=0.2,
        maximum_single_symbol_positive_edge_share=0.6,
        maximum_single_month_positive_edge_share=0.6,
        top_trade_count=5,
        minimum_top_trades_removed_edge_ratio=0.4,
        cost_stress_multipliers=(1.0, 2.0, 3.0),
        minimum_cost_stress_mean_net_return=0.001,
        delay_stress_multiplier=3.0,
        minimum_delay_stress_mean_net_return=0.001,
        spread_stress_multiplier=3.0,
        minimum_spread_stress_mean_net_return=0.001,
        basis_points_per_unit_return=10000.0,
        maximum_adv_participation_ratio=0.025,
        minimum_capacity_pass_fraction=1.0,
        minimum_market_neutral_edge_ratio=0.8,
        minimum_sector_neutral_edge_ratio=0.8,
        minimum_known_factor_neutral_edge_ratio=0.8,
        regression_variance_epsilon=1e-12,
        minimum_regime_observations=4,
        minimum_regime_pass_fraction=1.0,
        minimum_regime_mean_net_return=0.001,
        minimum_ablation_edge_ratio=0.7,
        minimum_ablation_pass_fraction=1.0,
        numeric_tolerance=1e-12,
    )


def _variant_catalog() -> tuple[tuple[str, str, str, str, str], ...]:
    return (
        (BASE_VARIANT_ID,) * 5,
        ("NEAR-1", BASE_VARIANT_ID, BASE_VARIANT_ID, BASE_VARIANT_ID, BASE_VARIANT_ID),
        (BASE_VARIANT_ID, "ABLATE-1", BASE_VARIANT_ID, BASE_VARIANT_ID, BASE_VARIANT_ID),
        (BASE_VARIANT_ID, BASE_VARIANT_ID, "SHIFT-1", BASE_VARIANT_ID, BASE_VARIANT_ID),
        (BASE_VARIANT_ID, BASE_VARIANT_ID, BASE_VARIANT_ID, "INVERT-1", BASE_VARIANT_ID),
        (BASE_VARIANT_ID, BASE_VARIANT_ID, BASE_VARIANT_ID, BASE_VARIANT_ID, "SHUFFLE-1"),
    )


def _observations() -> tuple[CandidateEvaluationObservationV1, ...]:
    rows: list[CandidateEvaluationObservationV1] = []
    first = datetime(2025, 1, 6, 15, 0, tzinfo=UTC)
    last_decision = first + timedelta(days=8 * 11)
    for session_index in range(12):
        decision_time = first + timedelta(days=8 * session_index)
        direction = 1.0 if session_index % 2 == 0 else -1.0
        for variants in _variant_catalog():
            if variants[0] != BASE_VARIANT_ID:
                candidate_return = 0.0018
            elif variants[1] != BASE_VARIANT_ID:
                candidate_return = 0.00175
            elif variants != (BASE_VARIANT_ID,) * 5:
                candidate_return = 0.0001
            else:
                candidate_return = 0.002
            for instrument_index, instrument_id in enumerate(("AAA", "BBB")):
                source_hash = canonical_hash(
                    {
                        "session": session_index,
                        "instrument": instrument_id,
                        "variants": variants,
                    }
                )
                rows.append(
                    CandidateEvaluationObservationV1(
                        observation_id=(
                            f"obs-{session_index}-{instrument_id}-"
                            + "-".join(variants)
                        ),
                        decision_time=decision_time,
                        signal_data_cutoff=decision_time - timedelta(minutes=15),
                        available_at=decision_time - timedelta(minutes=30),
                        source_event_time=decision_time - timedelta(minutes=45),
                        outcome_available_at=decision_time + timedelta(days=1),
                        constituent_membership_available_at=(
                            decision_time - timedelta(days=365)
                        ),
                        constituent_valid_from=first - timedelta(days=365),
                        constituent_valid_until=(
                            last_decision + timedelta(days=30)
                            if instrument_id == "BBB"
                            else None
                        ),
                        revision_available_at=decision_time - timedelta(minutes=40),
                        source_revision=0,
                        revision_was_known_at_cutoff=True,
                        instrument_id=instrument_id,
                        instrument_is_non_survivor=instrument_id == "BBB",
                        trade_id=f"trade-{session_index}",
                        candidate_score=1.0 - instrument_index * 0.1,
                        candidate_target=0.4,
                        candidate_return=candidate_return,
                        baseline_return=0.0,
                        modeled_cost=0.0001,
                        modeled_spread_bps=1.0,
                        modeled_delay_bps=1.0,
                        adv_usd=10_000_000.0,
                        capacity_used_usd=100_000.0,
                        market_return=direction * 0.01,
                        sector_return=-direction * 0.008,
                        known_factor_returns=(
                            KnownFactorReturnV1(
                                factor_id="MOM",
                                return_value=direction * 0.006,
                            ),
                            KnownFactorReturnV1(
                                factor_id="VALUE",
                                return_value=-direction * 0.004,
                            ),
                        ),
                        regime="BULL" if session_index % 2 == 0 else "BEAR",
                        parameter_neighborhood_id=variants[0],
                        data_ablation_id=variants[1],
                        date_shift_id=variants[2],
                        inversion_id=variants[3],
                        shuffle_id=variants[4],
                        source_hashes=(source_hash,),
                    )
                )
    return tuple(rows)


def _trace(
    *,
    observations: tuple[CandidateEvaluationObservationV1, ...] | None = None,
    contract: FalsificationEvaluationContractV1 | None = None,
    eligible_instrument_count: int = 2,
    eligible_non_survivor_count: int = 1,
) -> CandidateEvaluationTraceV1:
    rows = observations or _observations()
    return build_candidate_evaluation_trace(
        trace_id="trace-1",
        challenger_id="challenger-1",
        candidate_artifact_hash=ARTIFACT_HASH,
        data_manifest_hash=DATA_HASH,
        evaluation_contract=contract or _contract(),
        eligible_instrument_count=eligible_instrument_count,
        eligible_non_survivor_count=eligible_non_survivor_count,
        observations=rows,
        created_at=max(row.outcome_available_at for row in rows) + timedelta(days=1),
    )


def _context(trace: CandidateEvaluationTraceV1) -> FalsificationRunContext:
    return FalsificationRunContext(
        challenger_id=trace.challenger_id,
        candidate_artifact_hash=trace.candidate_artifact_hash,
        evaluation_contract_hash=trace.evaluation_contract_hash,
        data_manifest_hash=trace.data_manifest_hash,
        replay_hash=REPLAY_HASH,
        deterministic_seed=7077,
    )


def _budget() -> ExperimentBudget:
    return ExperimentBudget(
        experiment_family="test-family",
        submission_count=0,
        maximum_submissions=2,
        oos_budget_used=0,
        maximum_oos_budget=1,
    )


def _replace_rows(
    trace: CandidateEvaluationTraceV1,
    *,
    predicate: Callable[[CandidateEvaluationObservationV1], bool],
    update: Callable[
        [CandidateEvaluationObservationV1],
        dict[str, object],
    ],
) -> CandidateEvaluationTraceV1:
    rows = tuple(
        row.model_copy(update=update(row)) if predicate(row) else row
        for row in trace.observations
    )
    return _trace(
        observations=rows,
        contract=trace.evaluation_contract,
        eligible_instrument_count=trace.eligible_instrument_count,
        eligible_non_survivor_count=trace.eligible_non_survivor_count,
    )


def _evaluate(
    trace: CandidateEvaluationTraceV1,
    test_id: str,
) -> FalsificationStatus:
    evaluator = build_trusted_evaluator_factory(trace)[test_id]
    return evaluator.evaluate(test_id=test_id, context=_context(trace)).status


def test_trusted_factory_is_exactly_the_twenty_one_non_budget_tests() -> None:
    trace = _trace()
    evaluators = build_trusted_evaluator_factory(trace)
    assert set(evaluators) == TRACE_EVALUATOR_TEST_IDS
    assert set(evaluators) == set(MANDATORY_FALSIFICATION_TESTS) - {
        "experiment_budget"
    }
    assert len(evaluators) == 21


def test_complete_host_trace_passes_every_gate_deterministically() -> None:
    trace = _trace()
    first = AutomatedFalsificationRunner.from_host_trace(trace).run(
        context=_context(trace),
        budget=_budget(),
        created_at=trace.created_at,
    )
    second = AutomatedFalsificationRunner.from_host_trace(trace).run(
        context=_context(trace),
        budget=_budget(),
        created_at=trace.created_at,
    )
    assert first.mandatory_passed is True
    assert first.report_hash == second.report_hash
    assert all(result.status is FalsificationStatus.PASS for result in first.results)


def test_trace_hash_is_invariant_to_input_row_order() -> None:
    rows = _observations()
    assert _trace(observations=rows).trace_hash == _trace(
        observations=tuple(reversed(rows))
    ).trace_hash


@pytest.mark.parametrize(
    "test_id",
    [
        "parameter_instability",
        "date_shift_placebo",
        "signal_direction_inversion_placebo",
        "symbol_label_shuffle",
        "single_symbol_or_month_dependence",
        "top_five_trades_removed",
        "cost_stress_1x_2x_3x",
        "execution_delay_stress",
        "spread_widening_stress",
        "liquidity_capacity_stress",
        "market_beta_neutralization",
        "sector_beta_neutralization",
        "known_factor_neutralization",
        "regime_split",
        "parameter_neighborhood_stability",
        "partial_data_removal_sensitivity",
    ],
)
def test_statistical_gate_detects_its_adversarial_trace(test_id: str) -> None:
    trace = _adversarial_trace(test_id)
    assert _evaluate(trace, test_id) is FalsificationStatus.FAIL


def test_survivor_coverage_gate_fails_when_eligible_instrument_is_absent() -> None:
    trace = _trace(eligible_instrument_count=3)
    assert _evaluate(trace, "survivor_bias") is FalsificationStatus.FAIL


@pytest.mark.parametrize(
    ("test_id", "field", "value_factory"),
    [
        (
            "future_data_leakage",
            "available_at",
            lambda row: row.signal_data_cutoff + timedelta(seconds=1),
        ),
        (
            "pit_constituent_leakage",
            "constituent_membership_available_at",
            lambda row: row.signal_data_cutoff + timedelta(seconds=1),
        ),
        (
            "revised_data_backfill_leakage",
            "revision_was_known_at_cutoff",
            lambda row: False,
        ),
        (
            "lookahead_bias",
            "source_event_time",
            lambda row: row.signal_data_cutoff + timedelta(seconds=1),
        ),
    ],
)
def test_pit_gate_rejects_invalid_row_before_evaluation(
    test_id: str,
    field: str,
    value_factory: Callable[[CandidateEvaluationObservationV1], object],
) -> None:
    row = _observations()[0]
    payload = row.model_dump(mode="python")
    payload[field] = value_factory(row)
    with pytest.raises(ValidationError):
        CandidateEvaluationObservationV1.model_validate(payload)
    assert test_id in TRACE_EVALUATOR_TEST_IDS


def test_stale_record_is_rejected_by_trace_contract() -> None:
    rows = _observations()
    first = rows[0]
    stale = first.model_copy(
        update={
            "available_at": first.decision_time - timedelta(hours=3),
            "source_event_time": first.decision_time - timedelta(hours=4),
        }
    )
    with pytest.raises(ValidationError, match="stale source"):
        _trace(observations=(stale, *rows[1:]))


def test_long_only_no_leverage_and_unique_keys_fail_closed() -> None:
    row = _observations()[0]
    short_payload = row.model_dump(mode="python")
    short_payload["candidate_target"] = -0.1
    with pytest.raises(ValidationError):
        CandidateEvaluationObservationV1.model_validate(short_payload)

    leveraged_rows = tuple(
        item.model_copy(update={"candidate_target": 0.6})
        if item.is_base
        else item
        for item in _observations()
    )
    with pytest.raises(ValidationError, match="uses leverage"):
        _trace(observations=leveraged_rows)

    duplicate = row.model_copy(update={"observation_id": "duplicate-observation"})
    with pytest.raises(ValidationError, match="keys must be unique"):
        _trace(observations=(*_observations(), duplicate))


def test_context_binding_mismatch_is_blocked() -> None:
    trace = _trace()
    context = _context(trace)
    mismatched = FalsificationRunContext(
        challenger_id=context.challenger_id,
        candidate_artifact_hash="d" * 64,
        evaluation_contract_hash=context.evaluation_contract_hash,
        data_manifest_hash=context.data_manifest_hash,
        replay_hash=context.replay_hash,
        deterministic_seed=context.deterministic_seed,
    )
    result = build_trusted_evaluator_factory(trace)["future_data_leakage"].evaluate(
        test_id="future_data_leakage",
        context=mismatched,
    )
    assert result.status is FalsificationStatus.BLOCKED
    assert result.reason_code == "HOST_TRACE_BINDING_MISMATCH"


def _adversarial_trace(test_id: str) -> CandidateEvaluationTraceV1:
    trace = _trace()
    if test_id == "parameter_instability":
        return _replace_rows(
            trace,
            predicate=lambda row: row.parameter_neighborhood_id != BASE_VARIANT_ID,
            update=lambda row: {"candidate_return": 0.004},
        )
    if test_id == "parameter_neighborhood_stability":
        return _replace_rows(
            trace,
            predicate=lambda row: row.parameter_neighborhood_id != BASE_VARIANT_ID,
            update=lambda row: {"candidate_return": 0.0001},
        )
    if test_id == "partial_data_removal_sensitivity":
        return _replace_rows(
            trace,
            predicate=lambda row: row.data_ablation_id != BASE_VARIANT_ID,
            update=lambda row: {"candidate_return": 0.0001},
        )
    placebo_field = {
        "date_shift_placebo": "date_shift_id",
        "signal_direction_inversion_placebo": "inversion_id",
        "symbol_label_shuffle": "shuffle_id",
    }.get(test_id)
    if placebo_field is not None:
        return _replace_rows(
            trace,
            predicate=lambda row: getattr(row, placebo_field) != BASE_VARIANT_ID,
            update=lambda row: {"candidate_return": 0.002},
        )
    if test_id == "single_symbol_or_month_dependence":
        return _replace_rows(
            trace,
            predicate=lambda row: row.is_base,
            update=lambda row: {
                "candidate_return": 0.004 if row.instrument_id == "AAA" else 0.0001
            },
        )
    if test_id == "top_five_trades_removed":
        return _replace_rows(
            trace,
            predicate=lambda row: row.is_base,
            update=lambda row: {
                "candidate_return": (
                    0.02
                    if int(row.trade_id.removeprefix("trade-")) < 5
                    else 0.0001
                )
            },
        )
    if test_id == "cost_stress_1x_2x_3x":
        return _replace_rows(
            trace,
            predicate=lambda row: row.is_base,
            update=lambda row: {"modeled_cost": 0.001},
        )
    if test_id == "execution_delay_stress":
        return _replace_rows(
            trace,
            predicate=lambda row: row.is_base,
            update=lambda row: {"modeled_delay_bps": 100.0},
        )
    if test_id == "spread_widening_stress":
        return _replace_rows(
            trace,
            predicate=lambda row: row.is_base,
            update=lambda row: {"modeled_spread_bps": 100.0},
        )
    if test_id == "liquidity_capacity_stress":
        return _replace_rows(
            trace,
            predicate=lambda row: row.is_base,
            update=lambda row: {"capacity_used_usd": 500_000.0},
        )
    if test_id == "market_beta_neutralization":
        return _replace_rows(
            trace,
            predicate=lambda row: row.is_base,
            update=lambda row: {"market_return": 0.0038},
        )
    if test_id == "sector_beta_neutralization":
        return _replace_rows(
            trace,
            predicate=lambda row: row.is_base,
            update=lambda row: {"sector_return": 0.0038},
        )
    if test_id == "known_factor_neutralization":
        return _replace_rows(
            trace,
            predicate=lambda row: row.is_base,
            update=lambda row: {
                "known_factor_returns": (
                    KnownFactorReturnV1(factor_id="MOM", return_value=0.0038),
                    KnownFactorReturnV1(factor_id="VALUE", return_value=0.0),
                )
            },
        )
    if test_id == "regime_split":
        return _replace_rows(
            trace,
            predicate=lambda row: row.is_base and row.regime == "BEAR",
            update=lambda row: {"candidate_return": 0.0001},
        )
    raise AssertionError(f"unsupported adversarial trace: {test_id}")
