from __future__ import annotations

import pytest

from trading.research.prospective_outcomes import ProspectiveOutcomeError
from trading.runtime.prospective_outcome_monitor import (
    run_continuous_prospective_outcome_monitor,
)


def test_outcome_monitor_retries_waits_refreshes_bars_and_stops() -> None:
    attempts = iter(
        (
            ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_REQUEST_NOT_AVAILABLE"
            ),
            ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_BAR_NOT_AVAILABLE"
            ),
            ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_NOT_YET_FINALIZED"
            ),
            "outcome",
        )
    )
    emitted: list[tuple[str, int]] = []
    sleeps: list[float] = []
    refreshes: list[str] = []
    ticks = iter((100.0,))

    def collect() -> str:
        item = next(attempts)
        if isinstance(item, Exception):
            raise item
        return item

    count = run_continuous_prospective_outcome_monitor(
        collect=collect,
        on_outcome=lambda item, sequence: emitted.append(
            (item, sequence)
        ),
        poll_seconds=7,
        history_refresh_cooldown_seconds=60,
        refresh_history=lambda: refreshes.append("refresh"),
        maximum_outcomes=1,
        sleep=sleeps.append,
        monotonic=lambda: next(ticks),
    )

    assert count == 1
    assert emitted == [("outcome", 1)]
    assert sleeps == [7.0, 7.0, 7.0]
    assert refreshes == ["refresh"]


def test_outcome_monitor_rate_limits_history_refresh() -> None:
    attempts = iter(
        (
            ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_BAR_NOT_AVAILABLE"
            ),
            ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_BAR_NOT_AVAILABLE"
            ),
            ProspectiveOutcomeError(
                "PROSPECTIVE_OUTCOME_BAR_NOT_AVAILABLE"
            ),
            "done",
        )
    )
    ticks = iter((100.0, 130.0, 161.0))
    refreshes: list[float] = []

    def collect() -> str:
        item = next(attempts)
        if isinstance(item, Exception):
            raise item
        return item

    run_continuous_prospective_outcome_monitor(
        collect=collect,
        on_outcome=lambda _item, _sequence: None,
        poll_seconds=1,
        history_refresh_cooldown_seconds=60,
        refresh_history=lambda: refreshes.append(1.0),
        maximum_outcomes=1,
        sleep=lambda _seconds: None,
        monotonic=lambda: next(ticks),
    )

    assert refreshes == [1.0, 1.0]


def test_outcome_monitor_fails_closed_on_missed_data_window() -> None:
    def collect() -> str:
        raise ProspectiveOutcomeError(
            "PROSPECTIVE_OUTCOME_DATA_WINDOW_MISSED"
        )

    with pytest.raises(
        ProspectiveOutcomeError,
        match="PROSPECTIVE_OUTCOME_DATA_WINDOW_MISSED",
    ):
        run_continuous_prospective_outcome_monitor(
            collect=collect,
            on_outcome=lambda _item, _sequence: None,
            poll_seconds=1,
            history_refresh_cooldown_seconds=60,
            maximum_outcomes=1,
            sleep=lambda _seconds: None,
        )
