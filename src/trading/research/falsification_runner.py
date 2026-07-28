from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from pydantic import JsonValue

from trading.research.contracts import (
    FalsificationReportV1,
    FalsificationStatus,
    FalsificationTestResultV1,
)
from trading.research.falsification import (
    MANDATORY_FALSIFICATION_TESTS,
    ExperimentBudget,
    build_falsification_report,
    make_test_result,
)

if TYPE_CHECKING:
    from trading.research.evaluation_contracts import CandidateEvaluationTraceV1

_BUDGET_TEST_ID = "experiment_budget"
_EVALUATOR_TEST_IDS = frozenset(MANDATORY_FALSIFICATION_TESTS) - {
    _BUDGET_TEST_ID
}


@dataclass(frozen=True, slots=True)
class FalsificationRunContext:
    challenger_id: str
    candidate_artifact_hash: str
    evaluation_contract_hash: str
    data_manifest_hash: str
    replay_hash: str
    deterministic_seed: int


@dataclass(frozen=True, slots=True)
class FalsificationObservation:
    status: FalsificationStatus
    reason_code: str
    metrics: Mapping[str, JsonValue]


class FalsificationEvaluator(Protocol):
    def evaluate(
        self,
        *,
        test_id: str,
        context: FalsificationRunContext,
    ) -> FalsificationObservation: ...


class AutomatedFalsificationRunner:
    """Run host-owned falsification evaluators with fail-closed completeness."""

    def __init__(
        self,
        evaluators: Mapping[str, FalsificationEvaluator],
    ) -> None:
        unexpected = sorted(set(evaluators) - _EVALUATOR_TEST_IDS)
        if unexpected:
            raise ValueError(
                "unknown falsification evaluators: " + ",".join(unexpected)
            )
        self._evaluators = dict(evaluators)

    @classmethod
    def from_host_trace(
        cls,
        trace: CandidateEvaluationTraceV1,
    ) -> AutomatedFalsificationRunner:
        """Use the complete trusted evaluator catalog for production traces."""

        from trading.research.evaluators import build_trusted_evaluator_factory

        return cls(build_trusted_evaluator_factory(trace))

    def run(
        self,
        *,
        context: FalsificationRunContext,
        budget: ExperimentBudget,
        created_at: datetime,
    ) -> FalsificationReportV1:
        binding_metrics: dict[str, JsonValue] = {
            "candidate_artifact_hash": context.candidate_artifact_hash,
            "evaluation_contract_hash": context.evaluation_contract_hash,
            "data_manifest_hash": context.data_manifest_hash,
            "replay_hash": context.replay_hash,
            "deterministic_seed": context.deterministic_seed,
        }
        results: list[FalsificationTestResultV1] = []
        for test_id in MANDATORY_FALSIFICATION_TESTS:
            if test_id == _BUDGET_TEST_ID:
                results.append(
                    make_test_result(
                        test_id=test_id,
                        status=(
                            FalsificationStatus.PASS
                            if budget.available
                            else FalsificationStatus.FAIL
                        ),
                        reason_code=(
                            "EXPERIMENT_BUDGET_AVAILABLE"
                            if budget.available
                            else "EXPERIMENT_BUDGET_EXHAUSTED"
                        ),
                        metrics={
                            **binding_metrics,
                            "submission_count": budget.submission_count,
                            "maximum_submissions": budget.maximum_submissions,
                            "oos_budget_used": budget.oos_budget_used,
                            "maximum_oos_budget": budget.maximum_oos_budget,
                        },
                    )
                )
                continue
            evaluator = self._evaluators.get(test_id)
            if evaluator is None:
                observation = FalsificationObservation(
                    status=FalsificationStatus.BLOCKED,
                    reason_code="TRUSTED_EVALUATOR_MISSING",
                    metrics={},
                )
            else:
                try:
                    observation = evaluator.evaluate(
                        test_id=test_id,
                        context=context,
                    )
                except Exception:
                    observation = FalsificationObservation(
                        status=FalsificationStatus.BLOCKED,
                        reason_code="TRUSTED_EVALUATOR_ERROR",
                        metrics={},
                    )
            results.append(
                make_test_result(
                    test_id=test_id,
                    status=observation.status,
                    reason_code=observation.reason_code,
                    metrics={
                        **binding_metrics,
                        **dict(observation.metrics),
                    },
                )
            )
        return build_falsification_report(
            challenger_id=context.challenger_id,
            results=results,
            budget=budget,
            created_at=created_at,
        )
