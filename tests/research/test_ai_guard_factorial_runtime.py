from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading.domain.contracts import Fill
from trading.domain.enums import OrderSide
from trading.experiments.ai_guard_factorial_runtime import (
    apply_factorial_fill,
    factorial_state_hash,
    factorial_target_weights,
    initialize_factorial_paper_arms,
    plan_factorial_rebalance,
)

NOW = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)


def _arms():
    return initialize_factorial_paper_arms(
        starting_capital_usd=Decimal("100000"),
        effective_at=NOW,
        common_market_manifest_hash="a" * 64,
        forecast_hash="b" * 64,
        policy_version="operational-risk-v1",
        decision_schedule_version="research-daily-v1",
        execution_scenario_version="matched-paper-v1",
        cost_model_version="paper-cost-v1",
        config_manifest_hash="c" * 64,
    )


def _targets():
    return factorial_target_weights(
        base_weights={
            "QQQ": Decimal("0.60"),
            "USD_CASH": Decimal("0.40"),
        },
        guard_risk_multiplier=Decimal("0.50"),
        ai_risk_multiplier=Decimal("0.75"),
    )


def test_factorial_arms_have_independent_state_and_ledger() -> None:
    arms = _arms()
    assert len({id(arm.portfolio.positions) for arm in arms.values()}) == 4
    assert len({id(arm.ledger) for arm in arms.values()}) == 4
    assert all(arm.real_order_routing is False for arm in arms.values())
    assert all(arm.portfolio.cash_usd == Decimal("100000") for arm in arms.values())


def test_treatments_are_reduce_only_and_factorially_distinct() -> None:
    targets = _targets()
    assert targets["B0-VOL"]["QQQ"] == Decimal("0.60")
    assert targets["B3-GUARD"]["QQQ"] == Decimal("0.300")
    assert targets["B3-AI"]["QQQ"] == Decimal("0.4500")
    assert targets["B3-AI-GUARD"]["QQQ"] == Decimal("0.22500")
    assert all(sum(target.values()) == Decimal("1") for target in targets.values())
    with pytest.raises(ValueError, match="within"):
        factorial_target_weights(
            base_weights={"QQQ": Decimal("0.6"), "USD_CASH": Decimal("0.4")},
            guard_risk_multiplier=Decimal("1.01"),
            ai_risk_multiplier=Decimal("1"),
        )


def test_orders_fills_positions_and_ledgers_remain_arm_local() -> None:
    planned = plan_factorial_rebalance(
        arms=_arms(),
        targets=_targets(),
        prices={"QQQ": Decimal("500")},
        created_at=NOW,
        valid_until=NOW + timedelta(minutes=20),
        decision_scope="cycle-example",
        minimum_notional_usd=Decimal("25"),
    )
    before_other_hashes = {
        arm_id: factorial_state_hash({arm_id: arm})
        for arm_id, arm in planned.items()
        if arm_id != "B3-AI"
    }
    order = planned["B3-AI"].pending_orders[0].intent
    fill = Fill(
        fill_id="factorial-fill-1",
        order_intent_id=order.order_intent_id,
        arm_id="B3-AI",
        symbol="QQQ",
        side=OrderSide.BUY,
        quantity=order.quantity,
        price=Decimal("500"),
        commission_usd=Decimal("0"),
        execution_scenario_id="matched-paper-v1",
        effective_at=NOW + timedelta(minutes=1),
        created_at=NOW + timedelta(minutes=1),
    )
    updated = {
        **planned,
        "B3-AI": apply_factorial_fill(
            planned["B3-AI"],
            fill=fill,
            prices={"QQQ": Decimal("500")},
        ),
    }
    assert updated["B3-AI"].portfolio.positions["QQQ"] > 0
    assert updated["B3-AI"].pending_orders == ()
    assert len(updated["B3-AI"].ledger) == 2
    assert all(
        factorial_state_hash({arm_id: updated[arm_id]}) == before_hash
        for arm_id, before_hash in before_other_hashes.items()
    )


def test_identical_inputs_replay_to_identical_factorial_hash() -> None:
    kwargs = {
        "targets": _targets(),
        "prices": {"QQQ": Decimal("500")},
        "created_at": NOW,
        "valid_until": NOW + timedelta(minutes=20),
        "decision_scope": "cycle-example",
        "minimum_notional_usd": Decimal("25"),
    }
    first = plan_factorial_rebalance(arms=_arms(), **kwargs)
    second = plan_factorial_rebalance(arms=_arms(), **kwargs)
    assert factorial_state_hash(first) == factorial_state_hash(second)


def test_factorial_market_and_forecast_bindings_require_hashes() -> None:
    with pytest.raises(ValueError, match="market and forecast hashes"):
        replace(
            _arms()["B0-VOL"],
            common_market_manifest_hash="not-a-hash",
        )


def test_factorial_portfolio_must_reconcile_to_its_ledger() -> None:
    arm = _arms()["B0-VOL"]
    with pytest.raises(ValueError, match="reconcile to fills and ledger"):
        replace(
            arm,
            portfolio=replace(
                arm.portfolio,
                cash_usd=arm.portfolio.cash_usd - Decimal("1"),
            ),
        )
