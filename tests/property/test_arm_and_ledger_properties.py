from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from trading.domain.contracts import Fill
from trading.domain.enums import OrderSide
from trading.experiments.arms import ARM_IDS, create_arm_states, states_are_independent
from trading.ledger.journal import capital_entry, fill_entry, rebuild_holdings


@given(
    quantity=st.integers(min_value=1, max_value=1000),
    price_cents=st.integers(min_value=100, max_value=100_000),
    fee_cents=st.integers(min_value=0, max_value=500),
)
def test_fill_journal_is_balanced_and_rebuildable(
    quantity: int,
    price_cents: int,
    fee_cents: int,
) -> None:
    instant = datetime(2026, 7, 20, 19, 45, tzinfo=UTC)
    fill = Fill(
        fill_id=f"fill_{quantity}_{price_cents}_{fee_cents}",
        order_intent_id="order",
        arm_id="B1",
        symbol="QQQ",
        side=OrderSide.BUY,
        quantity=Decimal(quantity),
        price=Decimal(price_cents) / Decimal("100"),
        commission_usd=Decimal(fee_cents) / Decimal("100"),
        execution_scenario_id="property",
        effective_at=instant,
        created_at=instant,
    )
    entries = [capital_entry("B1", Decimal("1000000"), instant), fill_entry(fill)]
    cash, positions = rebuild_holdings(entries)
    assert positions["QQQ"] == Decimal(quantity)
    assert cash == Decimal("1000000") - fill.quantity * fill.price - fill.commission_usd


def test_shadow_arm_position_maps_are_not_aliased() -> None:
    states = create_arm_states(Decimal("100000"))
    assert states_are_independent(states)
    assert len({id(states[arm].positions) for arm in ARM_IDS}) == 7


def test_fill_for_one_arm_cannot_mutate_another() -> None:
    instant = datetime(2026, 7, 20, 19, 45, tzinfo=UTC)
    states = create_arm_states(Decimal("100000"))
    fill = Fill(
        fill_id="fill_isolated",
        order_intent_id="order",
        arm_id="B1",
        symbol="QQQ",
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        price=Decimal("500"),
        commission_usd=Decimal("0.50"),
        execution_scenario_id="property",
        effective_at=instant,
        created_at=instant,
    )
    updated_b1 = states["B1"].apply_fill(fill)
    assert updated_b1.positions == {"QQQ": Decimal("1")}
    assert all(states[arm].positions == {} for arm in ARM_IDS if arm != "B1")

