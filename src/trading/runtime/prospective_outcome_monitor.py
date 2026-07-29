from __future__ import annotations

import time
from collections.abc import Callable

from trading.research.prospective_outcomes import ProspectiveOutcomeError

PROSPECTIVE_OUTCOME_WAIT_ERROR_CODES = frozenset(
    {
        "PROSPECTIVE_OUTCOME_REQUEST_NOT_AVAILABLE",
        "PROSPECTIVE_OUTCOME_NOT_YET_AVAILABLE",
        "PROSPECTIVE_OUTCOME_NOT_YET_FINALIZED",
        "PROSPECTIVE_OUTCOME_BAR_NOT_AVAILABLE",
    }
)


def run_continuous_prospective_outcome_monitor[T](
    *,
    collect: Callable[[], T],
    on_outcome: Callable[[T, int], None],
    poll_seconds: int,
    history_refresh_cooldown_seconds: int,
    refresh_history: Callable[[], None] | None = None,
    maximum_outcomes: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Materialize every mature forward outcome in request order."""

    if poll_seconds <= 0:
        raise ValueError("prospective outcome poll interval must be positive")
    if history_refresh_cooldown_seconds <= 0:
        raise ValueError(
            "prospective outcome history refresh cooldown must be positive"
        )
    if maximum_outcomes is not None and maximum_outcomes <= 0:
        raise ValueError(
            "prospective outcome maximum outcomes must be positive"
        )
    outcome_count = 0
    last_history_refresh: float | None = None
    while True:
        try:
            result = collect()
        except ProspectiveOutcomeError as exc:
            code = str(exc)
            if code not in PROSPECTIVE_OUTCOME_WAIT_ERROR_CODES:
                raise
            if (
                code == "PROSPECTIVE_OUTCOME_BAR_NOT_AVAILABLE"
                and refresh_history is not None
            ):
                now = monotonic()
                if (
                    last_history_refresh is None
                    or now - last_history_refresh
                    >= history_refresh_cooldown_seconds
                ):
                    refresh_history()
                    last_history_refresh = now
            sleep(float(poll_seconds))
            continue
        outcome_count += 1
        on_outcome(result, outcome_count)
        if (
            maximum_outcomes is not None
            and outcome_count >= maximum_outcomes
        ):
            return outcome_count
