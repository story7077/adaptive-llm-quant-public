from __future__ import annotations

import pytest

from trading.research.contracts import ChallengerStatus
from trading.runtime.prospective_evaluation import (
    ProspectiveEvaluationRunResult,
)
from trading.runtime.prospective_evaluation_monitor import (
    run_prospective_evaluation_monitor,
)


def _result(status: str) -> ProspectiveEvaluationRunResult:
    return ProspectiveEvaluationRunResult(
        status=status,
        challenger_status=ChallengerStatus.PROPOSED,
        successful_forward_sessions=0,
        required_forward_sessions=126,
        terminal_failure_count=0,
    )


def test_monitor_polls_until_one_terminal_evaluation() -> None:
    results = iter(
        (
            _result("WAITING_FOR_FORWARD_OUTCOMES"),
            _result("WAITING_FOR_FORWARD_OUTCOMES"),
            _result("FALSIFICATION_RECORDED"),
        )
    )
    calls = 0
    sleeps: list[float] = []

    def evaluate() -> ProspectiveEvaluationRunResult:
        nonlocal calls
        calls += 1
        return next(results)

    result = run_prospective_evaluation_monitor(
        evaluate=evaluate,
        poll_seconds=17,
        sleep=sleeps.append,
    )

    assert result.status == "FALSIFICATION_RECORDED"
    assert calls == 3
    assert sleeps == [17.0, 17.0]


def test_monitor_rejects_nonpositive_poll_interval() -> None:
    with pytest.raises(
        ValueError,
        match="poll interval must be positive",
    ):
        run_prospective_evaluation_monitor(
            evaluate=lambda: _result("WAITING_FOR_FORWARD_OUTCOMES"),
            poll_seconds=0,
            sleep=lambda _: None,
        )
