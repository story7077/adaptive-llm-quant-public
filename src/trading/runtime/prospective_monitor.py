from __future__ import annotations

import time
from collections.abc import Callable

from trading.research.prospective import ProspectiveCandidateError

PROSPECTIVE_WAIT_ERROR_CODES = frozenset(
    {
        "PARENT_DECISION_NOT_AVAILABLE",
        "EVALUATION_ANCHOR_NOT_AVAILABLE",
    }
)


def run_continuous_prospective_monitor[T](
    *,
    collect: Callable[[], T],
    on_observation: Callable[[T, int], None],
    poll_seconds: int,
    maximum_observations: int | None = None,
    on_poll: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Collect every pending parent decision in order and wait for new ones."""

    if poll_seconds <= 0:
        raise ValueError("prospective monitor poll interval must be positive")
    if maximum_observations is not None and maximum_observations <= 0:
        raise ValueError(
            "prospective monitor maximum observations must be positive"
        )
    observation_count = 0
    while True:
        if on_poll is not None:
            on_poll()
        try:
            result = collect()
        except ProspectiveCandidateError as exc:
            if str(exc) not in PROSPECTIVE_WAIT_ERROR_CODES:
                raise
            sleep(float(poll_seconds))
            continue
        observation_count += 1
        on_observation(result, observation_count)
        if (
            maximum_observations is not None
            and observation_count >= maximum_observations
        ):
            return observation_count
