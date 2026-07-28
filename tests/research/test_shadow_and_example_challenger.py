from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading.experiments.ai_guard_factorial import factorial_arm_contracts
from trading.research.shadow import (
    ShadowArmIdentity,
    ShadowExecutionContract,
    calculate_ai_guard_factorial,
    require_matched_shadow_contract,
)
from trading.strategies.challengers.t1_v1_1_0 import (
    BreadthMemberObservation,
    revised_equal_weight_breadth,
)

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)


def test_champion_and_challenger_have_independent_matched_arms() -> None:
    contract = ShadowExecutionContract(
        market_input_manifest_hash="a" * 64,
        decision_schedule_version="schedule-v1",
        execution_scenario_version="execution-v1",
        cost_model_version="cost-v1",
        starting_capital_usd="100000.00",
        liquidity_policy_version="liquidity-v1",
    )
    champion = ShadowArmIdentity("champion", "T1", "1.0.0", contract)
    challenger = ShadowArmIdentity("challenger", "T1", "1.1.0", contract)
    require_matched_shadow_contract(champion, challenger)
    with pytest.raises(ValueError, match="independent"):
        require_matched_shadow_contract(champion, champion)


def test_ai_guard_factorial_separates_main_and_interaction_effects() -> None:
    result = calculate_ai_guard_factorial(
        b0_vol=[0.00, 0.01],
        b3_guard=[0.01, 0.02],
        b3_ai=[0.02, 0.03],
        b3_ai_guard=[0.035, 0.045],
    )
    assert result.guard_main_effect == pytest.approx(0.0125)
    assert result.ai_main_effect == pytest.approx(0.0225)
    assert result.ai_guard_interaction_effect == pytest.approx(0.005)
    contracts = factorial_arm_contracts()
    assert {item.arm_id for item in contracts} == {
        "B0-VOL",
        "B3-GUARD",
        "B3-AI",
        "B3-AI-GUARD",
    }
    assert all(item.real_order_routing is False for item in contracts)


def test_example_challenger_fails_closed_without_pit_coverage() -> None:
    observations = [
        BreadthMemberObservation(
            symbol="AAPL",
            universe_id="SYNTHETIC_INDEX",
            membership_effective=True,
            above_slow_average=True,
            positive_intermediate_return=True,
            available_at=NOW - timedelta(minutes=1),
        )
    ]
    with pytest.raises(ValueError, match="PIT_CONSTITUENT_COVERAGE"):
        revised_equal_weight_breadth(
            observations,
            universe_id="SYNTHETIC_INDEX",
            expected_members=10,
            data_available_cutoff=NOW,
            minimum_coverage=Decimal("0.90"),
        )
