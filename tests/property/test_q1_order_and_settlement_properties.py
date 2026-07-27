from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from trading.domain.enums import OrderSide
from trading.domain.q1 import OrderEventType, Q1ArmId
from trading.execution.order_state import (
    OrderDescriptor,
    OrderEventProvenance,
    Q1OrderClass,
    append_order_event,
    reduce_order_events,
)
from trading.settlement.service import (
    BusinessCalendar,
    SettlementPolicy,
    SettlementProvenance,
    apply_settlement_events,
    record_opening_settled_cash,
    record_sell_receivable,
    settle_due_receivables,
)

INSTANT = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)


def _order_provenance() -> OrderEventProvenance:
    return OrderEventProvenance(
        config_manifest_hash="c" * 64,
        code_version="q1_math_core_v1",
        model_version="deterministic",
        source_manifest_hash="s" * 64,
        worker_fence_token="worker-1",
        cycle_attempt_count=1,
    )


@given(parts=st.lists(st.integers(min_value=1, max_value=100), min_size=1, max_size=8))
def test_partial_fill_event_reduction_preserves_exact_quantity(parts: list[int]) -> None:
    total = sum(parts)
    order = OrderDescriptor(
        order_intent_id=f"order-{total}-{'-'.join(map(str, parts))}",
        arm_id="Q1-DET",
        portfolio_decision_id="decision",
        symbol="QQQ",
        side=OrderSide.BUY,
        quantity=Decimal(total),
        order_class=Q1OrderClass.NORMAL,
        created_at=INSTANT,
        valid_until=INSTANT + timedelta(minutes=20),
    )
    events = [
        append_order_event(
            order=order,
            existing_events=(),
            event_type=OrderEventType.CREATED,
            occurred_at=INSTANT,
            available_at=INSTANT,
            provenance=_order_provenance(),
        )
    ]
    for index, part in enumerate(parts):
        event_type = (
            OrderEventType.FILLED
            if index == len(parts) - 1
            else OrderEventType.PARTIALLY_FILLED
        )
        events.append(
            append_order_event(
                order=order,
                existing_events=tuple(events),
                event_type=event_type,
                quantity_delta=Decimal(part),
                commission_delta_usd=Decimal(part) / Decimal("100"),
                occurred_at=INSTANT + timedelta(minutes=index + 1),
                available_at=INSTANT + timedelta(minutes=index + 1),
                provenance=_order_provenance(),
                quote_id=f"quote-{index}",
            )
        )
    aggregate = reduce_order_events(order, tuple(events))

    assert aggregate.remaining_quantity == 0
    assert aggregate.cumulative_filled_quantity == Decimal(total)
    assert aggregate.cumulative_commission_usd == Decimal(total) / Decimal("100")
    assert aggregate.status is OrderEventType.FILLED


@given(
    gross_cents=st.integers(min_value=100, max_value=1_000_000),
    commission_cents=st.integers(min_value=0, max_value=99),
)
def test_settlement_changes_cash_classification_not_total_cash(
    gross_cents: int,
    commission_cents: int,
) -> None:
    gross = Decimal(gross_cents) / Decimal("100")
    commission = min(
        Decimal(commission_cents) / Decimal("100"),
        gross - Decimal("0.01"),
    )
    policy = SettlementPolicy(
        version="settlement-v1",
        calendar_version="calendar-v1",
        lag_business_sessions=1,
    )
    calendar = BusinessCalendar(
        version="calendar-v1",
        sessions=(date(2026, 7, 27), date(2026, 7, 28)),
    )
    provenance = SettlementProvenance(
        run_id="q1-property",
        source_cycle_id="cycle-property",
        config_manifest_hash="c" * 64,
        code_version="q1_math_core_v1",
        model_version="deterministic",
        source_manifest_hash="s" * 64,
        worker_fence_token="worker-1",
        cycle_attempt_count=1,
    )
    opening = record_opening_settled_cash(
        arm_id=Q1ArmId.Q1_DET,
        amount_usd=Decimal("1000"),
        effective_at=INSTANT,
        created_at=INSTANT,
        calendar_session_id="session-one",
        policy=policy,
        provenance=provenance,
    )
    sell = record_sell_receivable(
        arm_id=Q1ArmId.Q1_DET,
        fill_id=f"fill-{gross_cents}-{commission_cents}",
        trade_at=INSTANT + timedelta(minutes=1),
        trade_session=date(2026, 7, 27),
        fill_notional_usd=gross,
        commission_usd=commission,
        created_at=INSTANT + timedelta(minutes=1),
        calendar_session_id="session-one",
        policy=policy,
        calendar=calendar,
        provenance=provenance,
    )
    before = apply_settlement_events(
        events=(opening, sell),
        as_of=INSTANT + timedelta(hours=1),
    )
    next_session = INSTANT + timedelta(days=1)
    settlements = settle_due_receivables(
        events=(opening, sell),
        through_session=date(2026, 7, 28),
        effective_at=next_session,
        created_at=next_session,
        calendar_session_id="session-two",
        policy=policy,
        calendar=calendar,
        provenance=provenance,
    )
    after = apply_settlement_events(
        events=(opening, sell, *settlements),
        as_of=next_session,
    )

    assert before.total_cash_usd == after.total_cash_usd
    assert before.unsettled_receivables_usd == gross - commission
    assert after.unsettled_receivables_usd == 0
