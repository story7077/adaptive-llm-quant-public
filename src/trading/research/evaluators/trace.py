from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue

from trading.research.contracts import FalsificationStatus
from trading.research.evaluation_contracts import (
    BASE_VARIANT_ID,
    CandidateEvaluationObservationV1,
    CandidateEvaluationTraceV1,
)
from trading.research.falsification import MANDATORY_FALSIFICATION_TESTS
from trading.research.falsification_runner import (
    FalsificationEvaluator,
    FalsificationObservation,
    FalsificationRunContext,
)

_BUDGET_TEST_ID = "experiment_budget"
TRACE_EVALUATOR_TEST_IDS = frozenset(MANDATORY_FALSIFICATION_TESTS) - {
    _BUDGET_TEST_ID
}
_VARIANT_FIELDS = (
    "parameter_neighborhood_id",
    "data_ablation_id",
    "date_shift_id",
    "inversion_id",
    "shuffle_id",
)


class TraceEvaluationBlocked(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _SessionObservation:
    decision_time: datetime
    net_edge: float
    market_return: float
    sector_return: float
    known_factor_returns: Mapping[str, float]
    regime: str


class _TraceEvaluator:
    def __init__(
        self,
        *,
        test_id: str,
        trace: CandidateEvaluationTraceV1,
    ) -> None:
        self._test_id = test_id
        self._trace = trace

    def evaluate(
        self,
        *,
        test_id: str,
        context: FalsificationRunContext,
    ) -> FalsificationObservation:
        if test_id != self._test_id:
            return _blocked("TRACE_EVALUATOR_TEST_ID_MISMATCH")
        binding_failure = _binding_failure(self._trace, context)
        if binding_failure is not None:
            return binding_failure
        try:
            return _EVALUATION_METHODS[test_id](self._trace)
        except TraceEvaluationBlocked as exc:
            return _blocked(str(exc))


def build_trusted_evaluator_factory(
    trace: CandidateEvaluationTraceV1,
) -> dict[str, FalsificationEvaluator]:
    """Build the complete host-owned evaluator set for one immutable trace."""

    validated_trace = CandidateEvaluationTraceV1.model_validate(
        trace.model_dump(mode="python")
    )
    evaluators: dict[str, FalsificationEvaluator] = {
        test_id: _TraceEvaluator(test_id=test_id, trace=validated_trace)
        for test_id in sorted(TRACE_EVALUATOR_TEST_IDS)
    }
    if frozenset(evaluators) != TRACE_EVALUATOR_TEST_IDS:
        raise RuntimeError("trusted falsification evaluator catalog is incomplete")
    return evaluators


def _binding_failure(
    trace: CandidateEvaluationTraceV1,
    context: FalsificationRunContext,
) -> FalsificationObservation | None:
    bindings = (
        (trace.challenger_id, context.challenger_id),
        (trace.candidate_artifact_hash, context.candidate_artifact_hash),
        (trace.evaluation_contract_hash, context.evaluation_contract_hash),
        (trace.data_manifest_hash, context.data_manifest_hash),
    )
    if any(actual != expected for actual, expected in bindings):
        return _blocked("HOST_TRACE_BINDING_MISMATCH")
    return None


def _future_data_leakage(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    contract = trace.evaluation_contract
    future = sum(row.available_at > row.signal_data_cutoff for row in trace.observations)
    stale = sum(
        (row.decision_time - row.available_at).total_seconds()
        > contract.maximum_source_age_seconds
        for row in trace.observations
    )
    metrics: dict[str, JsonValue] = {
        "future_record_count": future,
        "stale_record_count": stale,
        "observation_count": len(trace.observations),
    }
    return _threshold_result(
        passed=future == 0 and stale == 0,
        pass_reason="PIT_AVAILABILITY_VALID",
        fail_reason="FUTURE_OR_STALE_DATA_DETECTED",
        metrics=metrics,
    )


def _pit_constituent_leakage(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    violations = sum(
        row.constituent_membership_available_at > row.signal_data_cutoff
        or row.constituent_valid_from > row.decision_time
        or (
            row.constituent_valid_until is not None
            and row.constituent_valid_until < row.decision_time
        )
        for row in trace.observations
    )
    return _threshold_result(
        passed=violations == 0,
        pass_reason="PIT_CONSTITUENT_MEMBERSHIP_VALID",
        fail_reason="PIT_CONSTITUENT_LEAKAGE_DETECTED",
        metrics={"violation_count": violations},
    )


def _revised_data_backfill_leakage(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    violations = sum(
        row.revision_available_at > row.signal_data_cutoff
        or not row.revision_was_known_at_cutoff
        for row in trace.observations
    )
    distinct_revisions = len(
        {
            (row.instrument_id, row.source_revision, row.revision_available_at)
            for row in trace.observations
        }
    )
    return _threshold_result(
        passed=violations == 0,
        pass_reason="PIT_SOURCE_REVISIONS_VALID",
        fail_reason="REVISED_DATA_BACKFILL_DETECTED",
        metrics={
            "violation_count": violations,
            "distinct_revision_count": distinct_revisions,
        },
    )


def _survivor_bias(trace: CandidateEvaluationTraceV1) -> FalsificationObservation:
    contract = trace.evaluation_contract
    base = _base_rows(trace)
    included = {row.instrument_id for row in base}
    non_survivors = {
        row.instrument_id for row in base if row.instrument_is_non_survivor
    }
    universe_coverage = len(included) / trace.eligible_instrument_count
    non_survivor_coverage = (
        len(non_survivors) / trace.eligible_non_survivor_count
        if trace.eligible_non_survivor_count
        else 1.0
    )
    passed = (
        universe_coverage + contract.numeric_tolerance
        >= contract.minimum_universe_coverage_ratio
        and non_survivor_coverage + contract.numeric_tolerance
        >= contract.minimum_non_survivor_coverage_ratio
    )
    return _threshold_result(
        passed=passed,
        pass_reason="UNIVERSE_SURVIVOR_COVERAGE_VALID",
        fail_reason="SURVIVOR_BIAS_COVERAGE_FAILED",
        metrics={
            "included_instrument_count": len(included),
            "eligible_instrument_count": trace.eligible_instrument_count,
            "included_non_survivor_count": len(non_survivors),
            "eligible_non_survivor_count": trace.eligible_non_survivor_count,
            "universe_coverage_ratio": universe_coverage,
            "non_survivor_coverage_ratio": non_survivor_coverage,
        },
    )


def _lookahead_bias(trace: CandidateEvaluationTraceV1) -> FalsificationObservation:
    violations = sum(
        row.source_event_time > row.signal_data_cutoff
        or row.outcome_available_at <= row.decision_time
        for row in trace.observations
    )
    return _threshold_result(
        passed=violations == 0,
        pass_reason="SIGNAL_AND_OUTCOME_TIMES_VALID",
        fail_reason="LOOKAHEAD_BIAS_DETECTED",
        metrics={"violation_count": violations},
    )


def _parameter_instability(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    base_mean = _positive_base_mean(trace)
    variants = _variant_means(trace, "parameter_neighborhood_id")
    maximum_deviation = max(
        abs(value - base_mean)
        / max(abs(base_mean), trace.evaluation_contract.numeric_tolerance)
        for value in variants.values()
    )
    threshold = trace.evaluation_contract.maximum_parameter_relative_deviation
    return _threshold_result(
        passed=maximum_deviation <= threshold + trace.evaluation_contract.numeric_tolerance,
        pass_reason="PARAMETER_STABILITY_VALID",
        fail_reason="PARAMETER_INSTABILITY_DETECTED",
        metrics={
            "base_mean_net_return": base_mean,
            "maximum_relative_deviation": maximum_deviation,
            "maximum_allowed_relative_deviation": threshold,
            "variant_means": dict(sorted(variants.items())),
        },
    )


def _date_shift_placebo(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    return _placebo_result(trace, "date_shift_id", "DATE_SHIFT")


def _signal_direction_inversion_placebo(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    return _placebo_result(trace, "inversion_id", "SIGNAL_DIRECTION_INVERSION")


def _symbol_label_shuffle(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    return _placebo_result(trace, "shuffle_id", "SYMBOL_LABEL_SHUFFLE")


def _single_symbol_or_month_dependence(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    contract = trace.evaluation_contract
    base = _base_rows(trace)
    by_symbol: dict[str, float] = defaultdict(float)
    by_month: dict[str, float] = defaultdict(float)
    for row in base:
        edge = _row_net_edge(row)
        by_symbol[row.instrument_id] += edge
        by_month[row.decision_time.strftime("%Y-%m")] += edge
    symbol_share = _maximum_positive_share(by_symbol, contract.numeric_tolerance)
    month_share = _maximum_positive_share(by_month, contract.numeric_tolerance)
    passed = (
        symbol_share
        <= contract.maximum_single_symbol_positive_edge_share
        + contract.numeric_tolerance
        and month_share
        <= contract.maximum_single_month_positive_edge_share
        + contract.numeric_tolerance
    )
    return _threshold_result(
        passed=passed,
        pass_reason="EDGE_CONCENTRATION_VALID",
        fail_reason="SINGLE_SYMBOL_OR_MONTH_DEPENDENCE_DETECTED",
        metrics={
            "maximum_symbol_positive_edge_share": symbol_share,
            "maximum_month_positive_edge_share": month_share,
            "maximum_allowed_symbol_share": (
                contract.maximum_single_symbol_positive_edge_share
            ),
            "maximum_allowed_month_share": (
                contract.maximum_single_month_positive_edge_share
            ),
        },
    )


def _top_five_trades_removed(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    contract = trace.evaluation_contract
    by_trade: dict[str, float] = defaultdict(float)
    for row in _base_rows(trace):
        by_trade[row.trade_id] += _row_net_edge(row)
    if len(by_trade) <= contract.top_trade_count:
        raise TraceEvaluationBlocked("INSUFFICIENT_TRADES_FOR_TOP_TRADE_REMOVAL")
    ordered = sorted(by_trade.values(), reverse=True)
    total = sum(ordered)
    if total <= contract.numeric_tolerance:
        raise TraceEvaluationBlocked("BASE_EDGE_NOT_POSITIVE")
    retained = sum(ordered[contract.top_trade_count :])
    ratio = retained / total
    return _threshold_result(
        passed=(
            ratio + contract.numeric_tolerance
            >= contract.minimum_top_trades_removed_edge_ratio
        ),
        pass_reason="TOP_TRADE_REMOVAL_STABLE",
        fail_reason="TOP_TRADE_DEPENDENCE_DETECTED",
        metrics={
            "trade_count": len(by_trade),
            "removed_trade_count": contract.top_trade_count,
            "retained_edge_ratio": ratio,
            "minimum_retained_edge_ratio": (
                contract.minimum_top_trades_removed_edge_ratio
            ),
        },
    )


def _cost_stress_1x_2x_3x(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    contract = trace.evaluation_contract
    base = _base_rows(trace)
    means = {
        str(multiplier): _mean(
            tuple(
                _session_edges(base, cost_multiplier=multiplier).values()
            )
        )
        for multiplier in contract.cost_stress_multipliers
    }
    passed = all(
        value + contract.numeric_tolerance
        >= contract.minimum_cost_stress_mean_net_return
        for value in means.values()
    )
    mean_metrics: dict[str, JsonValue] = dict(means)
    return _threshold_result(
        passed=passed,
        pass_reason="COST_STRESS_VALID",
        fail_reason="COST_STRESS_FAILED",
        metrics={
            "mean_net_returns": mean_metrics,
            "minimum_mean_net_return": contract.minimum_cost_stress_mean_net_return,
        },
    )


def _execution_delay_stress(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    contract = trace.evaluation_contract
    base = _base_rows(trace)
    stressed = _mean(
        tuple(
            _session_edges(
                base,
                delay_multiplier=contract.delay_stress_multiplier,
                basis_points_per_unit_return=contract.basis_points_per_unit_return,
            ).values()
        )
    )
    return _threshold_result(
        passed=(
            stressed + contract.numeric_tolerance
            >= contract.minimum_delay_stress_mean_net_return
        ),
        pass_reason="EXECUTION_DELAY_STRESS_VALID",
        fail_reason="EXECUTION_DELAY_STRESS_FAILED",
        metrics={
            "stressed_mean_net_return": stressed,
            "delay_stress_multiplier": contract.delay_stress_multiplier,
            "minimum_mean_net_return": (
                contract.minimum_delay_stress_mean_net_return
            ),
        },
    )


def _spread_widening_stress(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    contract = trace.evaluation_contract
    base = _base_rows(trace)
    stressed = _mean(
        tuple(
            _session_edges(
                base,
                spread_multiplier=contract.spread_stress_multiplier,
                basis_points_per_unit_return=contract.basis_points_per_unit_return,
            ).values()
        )
    )
    return _threshold_result(
        passed=(
            stressed + contract.numeric_tolerance
            >= contract.minimum_spread_stress_mean_net_return
        ),
        pass_reason="SPREAD_STRESS_VALID",
        fail_reason="SPREAD_STRESS_FAILED",
        metrics={
            "stressed_mean_net_return": stressed,
            "spread_stress_multiplier": contract.spread_stress_multiplier,
            "minimum_mean_net_return": (
                contract.minimum_spread_stress_mean_net_return
            ),
        },
    )


def _liquidity_capacity_stress(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    contract = trace.evaluation_contract
    exposed = [
        row
        for row in _base_rows(trace)
        if row.candidate_target > contract.numeric_tolerance
    ]
    if not exposed:
        raise TraceEvaluationBlocked("NO_EXPOSED_ROWS_FOR_CAPACITY_TEST")
    participation = tuple(row.capacity_used_usd / row.adv_usd for row in exposed)
    pass_fraction = (
        sum(
            value
            <= contract.maximum_adv_participation_ratio
            + contract.numeric_tolerance
            for value in participation
        )
        / len(participation)
    )
    return _threshold_result(
        passed=(
            pass_fraction + contract.numeric_tolerance
            >= contract.minimum_capacity_pass_fraction
        ),
        pass_reason="LIQUIDITY_CAPACITY_VALID",
        fail_reason="LIQUIDITY_CAPACITY_FAILED",
        metrics={
            "maximum_observed_participation": max(participation),
            "maximum_allowed_participation": (
                contract.maximum_adv_participation_ratio
            ),
            "capacity_pass_fraction": pass_fraction,
            "minimum_capacity_pass_fraction": (
                contract.minimum_capacity_pass_fraction
            ),
        },
    )


def _market_beta_neutralization(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    return _neutralization_result(
        trace,
        factor_kind="market",
        minimum_ratio=trace.evaluation_contract.minimum_market_neutral_edge_ratio,
        label="MARKET",
    )


def _sector_beta_neutralization(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    return _neutralization_result(
        trace,
        factor_kind="sector",
        minimum_ratio=trace.evaluation_contract.minimum_sector_neutral_edge_ratio,
        label="SECTOR",
    )


def _known_factor_neutralization(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    return _neutralization_result(
        trace,
        factor_kind="known",
        minimum_ratio=trace.evaluation_contract.minimum_known_factor_neutral_edge_ratio,
        label="KNOWN_FACTOR",
    )


def _regime_split(trace: CandidateEvaluationTraceV1) -> FalsificationObservation:
    contract = trace.evaluation_contract
    sessions = _base_sessions(trace)
    by_regime: dict[str, list[float]] = defaultdict(list)
    for session in sessions:
        by_regime[session.regime].append(session.net_edge)
    if any(
        len(values) < contract.minimum_regime_observations
        for values in by_regime.values()
    ):
        raise TraceEvaluationBlocked("INSUFFICIENT_REGIME_OBSERVATIONS")
    means = {
        regime: _mean(tuple(values))
        for regime, values in sorted(by_regime.items())
    }
    pass_fraction = (
        sum(
            value + contract.numeric_tolerance
            >= contract.minimum_regime_mean_net_return
            for value in means.values()
        )
        / len(means)
    )
    regime_metrics: dict[str, JsonValue] = dict(means)
    return _threshold_result(
        passed=(
            pass_fraction + contract.numeric_tolerance
            >= contract.minimum_regime_pass_fraction
        ),
        pass_reason="REGIME_SPLIT_VALID",
        fail_reason="REGIME_DEPENDENCE_DETECTED",
        metrics={
            "regime_means": regime_metrics,
            "regime_pass_fraction": pass_fraction,
            "minimum_regime_pass_fraction": contract.minimum_regime_pass_fraction,
            "minimum_regime_mean_net_return": (
                contract.minimum_regime_mean_net_return
            ),
        },
    )


def _parameter_neighborhood_stability(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    contract = trace.evaluation_contract
    base_mean = _positive_base_mean(trace)
    variants = _variant_means(trace, "parameter_neighborhood_id")
    pass_floor = base_mean * contract.minimum_neighborhood_edge_ratio
    pass_fraction = (
        sum(
            value + contract.numeric_tolerance >= pass_floor
            for value in variants.values()
        )
        / len(variants)
    )
    return _threshold_result(
        passed=(
            pass_fraction + contract.numeric_tolerance
            >= contract.minimum_neighborhood_pass_fraction
        ),
        pass_reason="PARAMETER_NEIGHBORHOOD_STABLE",
        fail_reason="PARAMETER_NEIGHBORHOOD_UNSTABLE",
        metrics={
            "base_mean_net_return": base_mean,
            "variant_means": dict(sorted(variants.items())),
            "pass_floor": pass_floor,
            "pass_fraction": pass_fraction,
            "minimum_pass_fraction": contract.minimum_neighborhood_pass_fraction,
        },
    )


def _partial_data_removal_sensitivity(
    trace: CandidateEvaluationTraceV1,
) -> FalsificationObservation:
    contract = trace.evaluation_contract
    base_mean = _positive_base_mean(trace)
    variants = _variant_means(trace, "data_ablation_id")
    pass_floor = base_mean * contract.minimum_ablation_edge_ratio
    pass_fraction = (
        sum(
            value + contract.numeric_tolerance >= pass_floor
            for value in variants.values()
        )
        / len(variants)
    )
    return _threshold_result(
        passed=(
            pass_fraction + contract.numeric_tolerance
            >= contract.minimum_ablation_pass_fraction
        ),
        pass_reason="PARTIAL_DATA_REMOVAL_STABLE",
        fail_reason="PARTIAL_DATA_REMOVAL_SENSITIVE",
        metrics={
            "base_mean_net_return": base_mean,
            "variant_means": dict(sorted(variants.items())),
            "pass_floor": pass_floor,
            "pass_fraction": pass_fraction,
            "minimum_pass_fraction": contract.minimum_ablation_pass_fraction,
        },
    )


def _placebo_result(
    trace: CandidateEvaluationTraceV1,
    field: str,
    label: str,
) -> FalsificationObservation:
    contract = trace.evaluation_contract
    base_mean = _positive_base_mean(trace)
    variants = _variant_means(trace, field)
    ratios = {
        variant_id: value / base_mean for variant_id, value in variants.items()
    }
    maximum_ratio = max(ratios.values())
    return _threshold_result(
        passed=(
            maximum_ratio
            <= contract.maximum_placebo_edge_ratio + contract.numeric_tolerance
        ),
        pass_reason=f"{label}_PLACEBO_VALID",
        fail_reason=f"{label}_PLACEBO_RETAINED_EDGE",
        metrics={
            "base_mean_net_return": base_mean,
            "variant_means": dict(sorted(variants.items())),
            "maximum_placebo_edge_ratio": maximum_ratio,
            "maximum_allowed_placebo_edge_ratio": (
                contract.maximum_placebo_edge_ratio
            ),
        },
    )


def _neutralization_result(
    trace: CandidateEvaluationTraceV1,
    *,
    factor_kind: str,
    minimum_ratio: float,
    label: str,
) -> FalsificationObservation:
    contract = trace.evaluation_contract
    sessions = _base_sessions(trace)
    edges = tuple(item.net_edge for item in sessions)
    base_mean = _mean(edges)
    if base_mean <= contract.minimum_base_mean_net_return:
        raise TraceEvaluationBlocked("BASE_EDGE_BELOW_CONTRACT_MINIMUM")
    if factor_kind == "market":
        factor_series = (tuple(item.market_return for item in sessions),)
    elif factor_kind == "sector":
        factor_series = (tuple(item.sector_return for item in sessions),)
    else:
        factor_ids = tuple(sessions[0].known_factor_returns)
        if not factor_ids:
            raise TraceEvaluationBlocked("KNOWN_FACTOR_SERIES_MISSING")
        if any(tuple(item.known_factor_returns) != factor_ids for item in sessions):
            raise TraceEvaluationBlocked("KNOWN_FACTOR_CATALOG_MISMATCH")
        factor_series = tuple(
            tuple(item.known_factor_returns[factor_id] for item in sessions)
            for factor_id in factor_ids
        )
    residual = edges
    betas: list[JsonValue] = []
    for values in factor_series:
        denominator = sum(value * value for value in values)
        if denominator <= contract.regression_variance_epsilon:
            beta = 0.0
        else:
            beta = sum(
                factor * edge for factor, edge in zip(values, residual, strict=True)
            ) / denominator
        residual = tuple(
            edge - beta * factor
            for edge, factor in zip(residual, values, strict=True)
        )
        betas.append(beta)
    neutral_mean = _mean(residual)
    ratio = neutral_mean / base_mean
    return _threshold_result(
        passed=ratio + contract.numeric_tolerance >= minimum_ratio,
        pass_reason=f"{label}_NEUTRALIZATION_VALID",
        fail_reason=f"{label}_DEPENDENCE_DETECTED",
        metrics={
            "base_mean_net_return": base_mean,
            "neutralized_mean_net_return": neutral_mean,
            "neutralized_edge_ratio": ratio,
            "minimum_neutralized_edge_ratio": minimum_ratio,
            "betas": betas,
        },
    )


def _variant_means(
    trace: CandidateEvaluationTraceV1,
    field: str,
) -> dict[str, float]:
    rows = _isolated_variant_rows(trace, field)
    grouped: dict[str, list[CandidateEvaluationObservationV1]] = defaultdict(list)
    for row in rows:
        grouped[str(getattr(row, field))].append(row)
    if not grouped:
        raise TraceEvaluationBlocked(f"{field.upper()}_VARIANTS_MISSING")
    base_session_count = len({row.decision_time for row in _base_rows(trace)})
    means: dict[str, float] = {}
    for variant_id, variant_rows in sorted(grouped.items()):
        session_edges = _session_edges(tuple(variant_rows))
        coverage = len(session_edges) / base_session_count
        if (
            coverage + trace.evaluation_contract.numeric_tolerance
            < trace.evaluation_contract.minimum_variant_session_coverage_ratio
        ):
            raise TraceEvaluationBlocked("VARIANT_SESSION_COVERAGE_INSUFFICIENT")
        means[variant_id] = _mean(tuple(session_edges.values()))
    return means


def _isolated_variant_rows(
    trace: CandidateEvaluationTraceV1,
    field: str,
) -> tuple[CandidateEvaluationObservationV1, ...]:
    if field not in _VARIANT_FIELDS:
        raise ValueError("unknown trace variant field")
    return tuple(
        row
        for row in trace.observations
        if getattr(row, field) != BASE_VARIANT_ID
        and all(
            getattr(row, other) == BASE_VARIANT_ID
            for other in _VARIANT_FIELDS
            if other != field
        )
    )


def _positive_base_mean(trace: CandidateEvaluationTraceV1) -> float:
    mean = _mean(tuple(_session_edges(_base_rows(trace)).values()))
    if (
        mean + trace.evaluation_contract.numeric_tolerance
        < trace.evaluation_contract.minimum_base_mean_net_return
    ):
        raise TraceEvaluationBlocked("BASE_EDGE_BELOW_CONTRACT_MINIMUM")
    if mean <= trace.evaluation_contract.numeric_tolerance:
        raise TraceEvaluationBlocked("BASE_EDGE_NOT_POSITIVE")
    return mean


def _base_rows(
    trace: CandidateEvaluationTraceV1,
) -> tuple[CandidateEvaluationObservationV1, ...]:
    rows = tuple(row for row in trace.observations if row.is_base)
    if len(rows) < trace.evaluation_contract.minimum_observation_count:
        raise TraceEvaluationBlocked("BASE_OBSERVATION_COUNT_INSUFFICIENT")
    sessions = {row.decision_time for row in rows}
    if len(sessions) < trace.evaluation_contract.minimum_session_count:
        raise TraceEvaluationBlocked("BASE_SESSION_COUNT_INSUFFICIENT")
    return rows


def _base_sessions(trace: CandidateEvaluationTraceV1) -> tuple[_SessionObservation, ...]:
    base = _base_rows(trace)
    edges = _session_edges(base)
    rows_by_session: dict[datetime, list[CandidateEvaluationObservationV1]] = defaultdict(list)
    for row in base:
        rows_by_session[row.decision_time].append(row)
    return tuple(
        _SessionObservation(
            decision_time=decision_time,
            net_edge=edges[decision_time],
            market_return=rows[0].market_return,
            sector_return=rows[0].sector_return,
            known_factor_returns={
                item.factor_id: item.return_value
                for item in rows[0].known_factor_returns
            },
            regime=rows[0].regime,
        )
        for decision_time, rows in sorted(rows_by_session.items())
    )


def _session_edges(
    rows: Sequence[CandidateEvaluationObservationV1],
    *,
    cost_multiplier: float = 1.0,
    delay_multiplier: float = 1.0,
    spread_multiplier: float = 1.0,
    basis_points_per_unit_return: float | None = None,
) -> dict[datetime, float]:
    grouped: dict[datetime, float] = defaultdict(float)
    for row in rows:
        edge = (
            row.candidate_return
            - row.baseline_return
            - cost_multiplier * row.modeled_cost
        )
        if basis_points_per_unit_return is not None:
            edge -= (
                (delay_multiplier - 1)
                * row.modeled_delay_bps
                * row.candidate_target
                / basis_points_per_unit_return
            )
            edge -= (
                (spread_multiplier - 1)
                * row.modeled_spread_bps
                * row.candidate_target
                / basis_points_per_unit_return
            )
        grouped[row.decision_time] += edge
    return dict(grouped)


def _row_net_edge(row: CandidateEvaluationObservationV1) -> float:
    return row.candidate_return - row.baseline_return - row.modeled_cost


def _maximum_positive_share(
    grouped_edges: Mapping[str, float],
    tolerance: float,
) -> float:
    positive = tuple(max(value, 0.0) for value in grouped_edges.values())
    total = sum(positive)
    if total <= tolerance:
        raise TraceEvaluationBlocked("POSITIVE_EDGE_MISSING")
    return max(positive) / total


def _mean(values: tuple[float, ...]) -> float:
    if not values:
        raise TraceEvaluationBlocked("EVALUATION_SERIES_EMPTY")
    return sum(values) / len(values)


def _threshold_result(
    *,
    passed: bool,
    pass_reason: str,
    fail_reason: str,
    metrics: Mapping[str, JsonValue],
) -> FalsificationObservation:
    return FalsificationObservation(
        status=(
            FalsificationStatus.PASS if passed else FalsificationStatus.FAIL
        ),
        reason_code=pass_reason if passed else fail_reason,
        metrics=metrics,
    )


def _blocked(reason_code: str) -> FalsificationObservation:
    return FalsificationObservation(
        status=FalsificationStatus.BLOCKED,
        reason_code=reason_code,
        metrics={},
    )


_EvaluationFunction = Callable[[CandidateEvaluationTraceV1], FalsificationObservation]
_EVALUATION_METHODS: dict[str, _EvaluationFunction] = {
    "future_data_leakage": _future_data_leakage,
    "pit_constituent_leakage": _pit_constituent_leakage,
    "revised_data_backfill_leakage": _revised_data_backfill_leakage,
    "survivor_bias": _survivor_bias,
    "lookahead_bias": _lookahead_bias,
    "parameter_instability": _parameter_instability,
    "date_shift_placebo": _date_shift_placebo,
    "signal_direction_inversion_placebo": _signal_direction_inversion_placebo,
    "symbol_label_shuffle": _symbol_label_shuffle,
    "single_symbol_or_month_dependence": _single_symbol_or_month_dependence,
    "top_five_trades_removed": _top_five_trades_removed,
    "cost_stress_1x_2x_3x": _cost_stress_1x_2x_3x,
    "execution_delay_stress": _execution_delay_stress,
    "spread_widening_stress": _spread_widening_stress,
    "liquidity_capacity_stress": _liquidity_capacity_stress,
    "market_beta_neutralization": _market_beta_neutralization,
    "sector_beta_neutralization": _sector_beta_neutralization,
    "known_factor_neutralization": _known_factor_neutralization,
    "regime_split": _regime_split,
    "parameter_neighborhood_stability": _parameter_neighborhood_stability,
    "partial_data_removal_sensitivity": _partial_data_removal_sensitivity,
}
if frozenset(_EVALUATION_METHODS) != TRACE_EVALUATOR_TEST_IDS:
    missing = sorted(TRACE_EVALUATOR_TEST_IDS - frozenset(_EVALUATION_METHODS))
    extra = sorted(frozenset(_EVALUATION_METHODS) - TRACE_EVALUATOR_TEST_IDS)
    raise RuntimeError(
        "trusted falsification evaluator catalog mismatch; "
        f"missing={','.join(missing)} extra={','.join(extra)}"
    )
