from __future__ import annotations

import time
from collections.abc import Callable

from trading.runtime.prospective_evaluation import (
    ProspectiveEvaluationRunResult,
)


def run_prospective_evaluation_monitor(
    *,
    evaluate: Callable[[], ProspectiveEvaluationRunResult],
    poll_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
) -> ProspectiveEvaluationRunResult:
    """Wait for the frozen cohort, evaluate it once, then stop."""

    if poll_seconds <= 0:
        raise ValueError(
            "prospective evaluation poll interval must be positive"
        )
    while True:
        result = evaluate()
        if result.status != "WAITING_FOR_FORWARD_OUTCOMES":
            return result
        sleep(float(poll_seconds))
