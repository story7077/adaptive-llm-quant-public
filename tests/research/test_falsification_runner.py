from __future__ import annotations

from datetime import UTC, datetime

from trading.research.contracts import FalsificationStatus
from trading.research.falsification import (
    MANDATORY_FALSIFICATION_TESTS,
    ExperimentBudget,
)
from trading.research.falsification_runner import (
    AutomatedFalsificationRunner,
    FalsificationObservation,
    FalsificationRunContext,
)

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)


class _PassingEvaluator:
    def evaluate(
        self,
        *,
        test_id: str,
        context: FalsificationRunContext,
    ) -> FalsificationObservation:
        del context
        return FalsificationObservation(
            status=FalsificationStatus.PASS,
            reason_code="HOST_CHECK_PASSED",
            metrics={"test_id_seen": test_id},
        )


class _FailingEvaluator:
    def evaluate(
        self,
        *,
        test_id: str,
        context: FalsificationRunContext,
    ) -> FalsificationObservation:
        del test_id, context
        raise RuntimeError("private evaluator detail must not escape")


def _context() -> FalsificationRunContext:
    return FalsificationRunContext(
        challenger_id="challenger-1",
        candidate_artifact_hash="a" * 64,
        evaluation_contract_hash="b" * 64,
        data_manifest_hash="c" * 64,
        replay_hash="d" * 64,
        deterministic_seed=7077,
    )


def _budget() -> ExperimentBudget:
    return ExperimentBudget(
        experiment_family="family",
        submission_count=0,
        maximum_submissions=2,
        oos_budget_used=0,
        maximum_oos_budget=1,
    )


def test_missing_trusted_evaluators_fail_closed() -> None:
    report = AutomatedFalsificationRunner({}).run(
        context=_context(),
        budget=_budget(),
        created_at=NOW,
    )
    assert report.mandatory_passed is False
    missing = [
        result
        for result in report.results
        if result.reason_code == "TRUSTED_EVALUATOR_MISSING"
    ]
    assert len(missing) == len(MANDATORY_FALSIFICATION_TESTS) - 1
    assert all(result.status is FalsificationStatus.BLOCKED for result in missing)


def test_registered_host_evaluators_produce_bound_deterministic_report() -> None:
    evaluator = _PassingEvaluator()
    evaluators = {
        test_id: evaluator
        for test_id in MANDATORY_FALSIFICATION_TESTS
        if test_id != "experiment_budget"
    }
    runner = AutomatedFalsificationRunner(evaluators)
    first = runner.run(context=_context(), budget=_budget(), created_at=NOW)
    second = runner.run(context=_context(), budget=_budget(), created_at=NOW)
    assert first.mandatory_passed is True
    assert first.report_hash == second.report_hash
    assert all(
        result.metrics["candidate_artifact_hash"] == "a" * 64
        and result.metrics["evaluation_contract_hash"] == "b" * 64
        and result.metrics["replay_hash"] == "d" * 64
        for result in first.results
    )


def test_evaluator_exception_is_sanitized_and_blocked() -> None:
    evaluator = _FailingEvaluator()
    report = AutomatedFalsificationRunner(
        {"future_data_leakage": evaluator}
    ).run(
        context=_context(),
        budget=_budget(),
        created_at=NOW,
    )
    result = next(
        item for item in report.results if item.test_id == "future_data_leakage"
    )
    assert result.status is FalsificationStatus.BLOCKED
    assert result.reason_code == "TRUSTED_EVALUATOR_ERROR"
    assert "private evaluator detail" not in str(result.model_dump(mode="json"))
