from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from trading.data.ports import available_as_of
from trading.data.synthetic import SyntheticBar, build_demo_scenario
from trading.domain.hashing import canonical_hash


def test_future_record_does_not_change_past_query() -> None:
    scenario = build_demo_scenario()
    before = canonical_hash(available_as_of(scenario.bars, scenario.decision_time))
    future = SyntheticBar(
        symbol="QQQ",
        event_time=scenario.decision_time + timedelta(days=365),
        available_at=scenario.decision_time + timedelta(days=365),
        open=Decimal("9999"),
        close=Decimal("9999"),
    )
    after = canonical_hash(
        available_as_of([*scenario.bars, future], scenario.decision_time)
    )
    assert before == after

