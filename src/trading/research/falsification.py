from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue

from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.research.contracts import (
    FalsificationReportV1,
    FalsificationStatus,
    FalsificationTestResultV1,
)

MANDATORY_FALSIFICATION_TESTS = (
    "future_data_leakage",
    "pit_constituent_leakage",
    "revised_data_backfill_leakage",
    "survivor_bias",
    "lookahead_bias",
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
    "experiment_budget",
)


class FalsificationGateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExperimentBudget:
    experiment_family: str
    submission_count: int
    maximum_submissions: int
    oos_budget_used: int
    maximum_oos_budget: int

    @property
    def available(self) -> bool:
        return (
            self.submission_count < self.maximum_submissions
            and self.oos_budget_used < self.maximum_oos_budget
        )


def build_falsification_report(
    *,
    challenger_id: str,
    results: Iterable[FalsificationTestResultV1],
    budget: ExperimentBudget,
    created_at: datetime,
) -> FalsificationReportV1:
    timestamp = require_aware_utc(created_at)
    provided_results = tuple(results)
    result_by_id = {result.test_id: result for result in provided_results}
    if len(result_by_id) != len(provided_results):
        raise FalsificationGateError("duplicate falsification result")
    missing = sorted(set(MANDATORY_FALSIFICATION_TESTS) - set(result_by_id))
    extra_mandatory = sorted(
        result.test_id
        for result in result_by_id.values()
        if result.mandatory and result.test_id not in MANDATORY_FALSIFICATION_TESTS
    )
    if missing:
        raise FalsificationGateError(f"missing mandatory tests: {','.join(missing)}")
    if extra_mandatory:
        raise FalsificationGateError(
            f"unknown mandatory tests: {','.join(extra_mandatory)}"
        )
    budget_result = result_by_id["experiment_budget"]
    expected_budget_status = (
        FalsificationStatus.PASS if budget.available else FalsificationStatus.FAIL
    )
    if budget_result.status is not expected_budget_status:
        raise FalsificationGateError("experiment budget result disagrees with ledger")
    ordered = [result_by_id[test_id] for test_id in MANDATORY_FALSIFICATION_TESTS]
    optional = sorted(
        (
            result
            for test_id, result in result_by_id.items()
            if test_id not in MANDATORY_FALSIFICATION_TESTS
        ),
        key=lambda item: item.test_id,
    )
    finalized = ordered + optional
    passed = all(
        (not result.mandatory) or result.status is FalsificationStatus.PASS
        for result in finalized
    )
    payload = {
        "schema_version": "falsification_report_v1",
        "challenger_id": challenger_id,
        "results": [result.model_dump(mode="json") for result in finalized],
        "mandatory_passed": passed,
        "created_at": timestamp,
    }
    return FalsificationReportV1.model_validate(
        {
            **payload,
            "report_hash": canonical_hash(payload),
        }
    )


def make_test_result(
    *,
    test_id: str,
    status: FalsificationStatus,
    reason_code: str,
    metrics: dict[str, JsonValue],
    mandatory: bool = True,
) -> FalsificationTestResultV1:
    payload = {
        "test_id": test_id,
        "mandatory": mandatory,
        "status": status,
        "reason_code": reason_code,
        "metrics": metrics,
    }
    return FalsificationTestResultV1.model_validate(
        {
            **payload,
            "result_hash": canonical_hash(payload),
        }
    )
