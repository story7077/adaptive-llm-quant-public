from __future__ import annotations

from collections import deque

import pytest

from trading.research.prospective import ProspectiveCandidateError
from trading.runtime.prospective_monitor import (
    run_continuous_prospective_monitor,
)


def test_monitor_waits_and_emits_each_observation_once_in_order() -> None:
    sequence: deque[str | ProspectiveCandidateError] = deque(
        (
            ProspectiveCandidateError("PARENT_DECISION_NOT_AVAILABLE"),
            "parent-decision-1",
            ProspectiveCandidateError("PARENT_DECISION_NOT_AVAILABLE"),
            "parent-decision-2",
        )
    )
    observed: list[tuple[int, str]] = []
    sleeps: list[float] = []
    polls: list[int] = []

    def collect() -> str:
        item = sequence.popleft()
        if isinstance(item, ProspectiveCandidateError):
            raise item
        return item

    count = run_continuous_prospective_monitor(
        collect=collect,
        on_observation=lambda item, index: observed.append((index, item)),
        poll_seconds=7,
        maximum_observations=2,
        on_poll=lambda: polls.append(len(polls) + 1),
        sleep=sleeps.append,
    )

    assert count == 2
    assert observed == [
        (1, "parent-decision-1"),
        (2, "parent-decision-2"),
    ]
    assert sleeps == [7.0, 7.0]
    assert polls == [1, 2, 3, 4]


def test_monitor_fails_closed_on_non_waiting_candidate_error() -> None:
    sleeps: list[float] = []

    with pytest.raises(
        ProspectiveCandidateError,
        match="COMMANDER_CANDIDATE_NONDETERMINISTIC",
    ):
        run_continuous_prospective_monitor(
            collect=lambda: _raise_candidate_error(
                "COMMANDER_CANDIDATE_NONDETERMINISTIC"
            ),
            on_observation=lambda _item, _index: None,
            poll_seconds=5,
            maximum_observations=1,
            sleep=sleeps.append,
        )

    assert sleeps == []


@pytest.mark.parametrize(
    ("poll_seconds", "maximum_observations"),
    ((0, 1), (1, 0)),
)
def test_monitor_rejects_invalid_operating_bounds(
    poll_seconds: int,
    maximum_observations: int,
) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        run_continuous_prospective_monitor(
            collect=lambda: "unused",
            on_observation=lambda _item, _index: None,
            poll_seconds=poll_seconds,
            maximum_observations=maximum_observations,
            sleep=lambda _seconds: None,
        )


def _raise_candidate_error(code: str) -> str:
    raise ProspectiveCandidateError(code)
