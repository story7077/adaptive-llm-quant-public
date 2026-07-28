# pyright: reportPrivateUsage=false

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from trading.domain.contracts import Fill
from trading.domain.enums import OrderSide
from trading.execution.q1_paper import (
    Q1ExecutionConfig,
    Q1PriceGuardViolation,
    build_q1_fill_economics,
)
from trading.llm.q1_overlay import (
    Q1_ALLOWED_RISK_MULTIPLIERS,
    Q1_TARGET_WEIGHT_SUM_TOLERANCE,
    Q1LlmOverlayDecision,
    Q1OverlayState,
    apply_reduce_only_overlay,
)
from trading.runtime.q1_paper import _llm_overlay_status
from trading.runtime.q1_scheduler import (
    Q1SessionSchedule,
    VersionedMarketSession,
    build_q1_session_slots,
    normal_order_valid_until,
)
from trading.runtime.q1_state import Q1ArmState, UnsettledReceivable
from trading.settings import load_q1_config_bundle


def test_q1_scheduler_respects_early_close() -> None:
    session = VersionedMarketSession(
        calendar_session_id="calendar-early",
        calendar_version="calendar-v1",
        session_date=date(2026, 11, 27),
        open_at=datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
        close_at=datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
        source_payload_hash="source",
        source_available_at=datetime(2026, 11, 20, tzinfo=UTC),
    )
    schedule = _schedule()

    slots = build_q1_session_slots(session, schedule=schedule)

    execution_times = [
        item.scheduled_at
        for item in slots
        if item.cycle_kind == "Q1_EXECUTION"
    ]
    assert execution_times
    assert all(value < session.close_at for value in execution_times)
    assert max(execution_times) == datetime(2026, 11, 27, 17, 59, tzinfo=UTC)
    assert normal_order_valid_until(session, schedule=schedule) == datetime(
        2026, 11, 27, 15, 20, tzinfo=UTC
    )


def test_q1_scheduler_uses_normal_1600_et_close() -> None:
    session = VersionedMarketSession(
        calendar_session_id="calendar-normal",
        calendar_version="calendar-v1",
        session_date=date(2026, 7, 27),
        open_at=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 7, 27, 20, 0, tzinfo=UTC),
        source_payload_hash="source",
        source_available_at=datetime(2026, 7, 20, tzinfo=UTC),
    )

    slots = build_q1_session_slots(session, schedule=_schedule())
    execution_times = [
        item.scheduled_at
        for item in slots
        if item.cycle_kind == "Q1_EXECUTION"
    ]

    assert execution_times
    assert max(execution_times) == datetime(
        2026,
        7,
        27,
        19,
        59,
        tzinfo=UTC,
    )
    assert all(value < session.close_at for value in execution_times)


def test_q1_status_materializes_expired_overlay_without_creating_buys() -> None:
    decision = cast(
        Any,
        SimpleNamespace(
            diagnostics={
                "llm_overlay_state": "ACTIVE",
                "llm_policy_expiry_time": (
                    "2026-07-27T16:00:00+00:00"
                ),
            }
        ),
    )

    assert (
        _llm_overlay_status(
            arm_id="Q1-LLM",
            decision=decision,
            status_now=datetime(2026, 7, 27, 16, 1, tzinfo=UTC),
        )
        == "EXPIRED_AWAITING_NEXT_REBALANCE"
    )


def test_q1_sell_fill_creates_unsettled_cash_not_buying_power() -> None:
    state = Q1ArmState(
        arm_id="Q1-DET",
        initial_nav_usd=Decimal("1000"),
        settled_cash_usd=Decimal("0"),
        unsettled_receivables=(),
        positions={"QQQ": Decimal("2")},
        sequence=0,
        evaluation_anchor_id="anchor",
    )
    fill = Fill(
        fill_id="fill-1",
        order_intent_id="order-1",
        arm_id="Q1-DET",
        symbol="QQQ",
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        price=Decimal("100"),
        commission_usd=Decimal("0.10"),
        execution_scenario_id="q1",
        effective_at=datetime(2026, 7, 27, 14, 2, tzinfo=UTC),
        created_at=datetime(2026, 7, 27, 14, 2, tzinfo=UTC),
    )
    receivable = UnsettledReceivable(
        receivable_id="receivable-1",
        source_fill_id=fill.fill_id,
        amount_usd=Decimal("99.90"),
        settlement_date=date(2026, 7, 28),
        created_at=fill.created_at,
    )

    sold = state.apply_fill(fill, sell_receivable=receivable)

    assert sold.settled_cash_usd == 0
    assert sold.unsettled_cash_usd == Decimal("99.90")
    assert sold.total_cash_usd == Decimal("99.90")
    with pytest.raises(ValueError, match="settled cash"):
        sold.apply_fill(
            fill.model_copy(
                update={
                    "fill_id": "buy-fill",
                    "order_intent_id": "buy-order",
                    "side": OrderSide.BUY,
                }
            )
        )
    settled = sold.settle(receivable.receivable_id)
    assert settled.settled_cash_usd == Decimal("99.90")
    assert settled.unsettled_cash_usd == 0


def test_q1_arm_weights_preserve_decimal_precision() -> None:
    state = Q1ArmState(
        arm_id="Q1-DET",
        initial_nav_usd=Decimal("1.000000000000000003"),
        settled_cash_usd=Decimal("0.000000000000000001"),
        unsettled_receivables=(),
        positions={"QQQ": Decimal("1")},
        sequence=0,
        evaluation_anchor_id="anchor",
    )

    weights = state.weights({"QQQ": Decimal("1.000000000000000002")})

    assert weights["QQQ"] == Decimal("1.000000000000000002") / Decimal(
        "1.000000000000000003"
    )
    assert weights["USD_CASH"] == Decimal("0.000000000000000001") / Decimal(
        "1.000000000000000003"
    )
    assert all(isinstance(value, Decimal) for value in weights.values())


def test_q1_overlay_is_reduce_only_and_expiry_does_not_restore_intraday() -> None:
    decision = Q1LlmOverlayDecision(
        request_id="request",
        context_manifest_hash="a" * 64,
        risk_multiplier=0.5,
        block_new_entries=True,
        evidence_event_ids=["news-1"],
        rationale="Reduce risk.",
        effective_time=datetime(2026, 7, 27, 16, 0, tzinfo=UTC),
        expiry_time=datetime(2026, 7, 27, 17, 0, tzinfo=UTC),
        created_at=datetime(2026, 7, 27, 15, 59, tzinfo=UTC),
    )
    base = {"QQQ": 0.6, "SOXX": 0.3, "USD_CASH": 0.1}

    active, state = apply_reduce_only_overlay(
        base,
        current_weights={"QQQ": 0.2, "SOXX": 0.1, "USD_CASH": 0.7},
        decision=decision,
        as_of=datetime(2026, 7, 27, 16, 30, tzinfo=UTC),
    )
    expired, expired_state = apply_reduce_only_overlay(
        base,
        current_weights=active,
        decision=decision,
        as_of=datetime(2026, 7, 27, 17, 1, tzinfo=UTC),
    )

    assert state is Q1OverlayState.ACTIVE
    assert active == {"QQQ": 0.2, "SOXX": 0.1, "USD_CASH": 0.7}
    assert expired_state is Q1OverlayState.EXPIRED_AWAITING_NEXT_REBALANCE
    assert expired == active


def test_q1_overlay_trading_numbers_match_versioned_config(
    repository_root: Path,
) -> None:
    document = load_q1_config_bundle(repository_root / "config").document
    llm = document["llm"]

    assert tuple(float(value) for value in llm["allowed_risk_multipliers"]) == (
        Q1_ALLOWED_RISK_MULTIPLIERS
    )
    assert Decimal(str(llm["target_weight_sum_tolerance"])) == (
        Q1_TARGET_WEIGHT_SUM_TOLERANCE
    )


def test_q1_execution_uses_dynamic_price_guard_and_caps() -> None:
    config = _execution_config()
    result = build_q1_fill_economics(
        side=OrderSide.BUY,
        remaining_quantity=Decimal("100"),
        bid_price=Decimal("99.90"),
        ask_price=Decimal("100.10"),
        executable_side_quantity=Decimal("100"),
        remaining_adv_capacity=Decimal("8"),
        decision_reference_price=Decimal("100"),
        decision_spread_bps=Decimal("20"),
        cumulative_notional_before=Decimal("0"),
        cumulative_commission_before=Decimal("0"),
        config=config,
    )

    assert result.quantity == Decimal("8.000000")
    assert result.guard_bps == Decimal("60")
    assert set(result.sensitivity_costs_usd) == {
        "plus_5_bps",
        "plus_10_bps",
    }
    with pytest.raises(Q1PriceGuardViolation):
        build_q1_fill_economics(
            side=OrderSide.BUY,
            remaining_quantity=Decimal("1"),
            bid_price=Decimal("100.90"),
            ask_price=Decimal("101.00"),
            executable_side_quantity=Decimal("100"),
            remaining_adv_capacity=Decimal("100"),
            decision_reference_price=Decimal("100"),
            decision_spread_bps=Decimal("5"),
            cumulative_notional_before=Decimal("0"),
            cumulative_commission_before=Decimal("0"),
            config=config,
        )


def _schedule() -> Q1SessionSchedule:
    return Q1SessionSchedule(
        first_nav_time_et=time(9, 45),
        nav_interval_minutes=15,
        strategic_time_et=time(10, 0),
        llm_review_times_et=(time(10, 0), time(12, 0)),
        normal_execution_start_et=time(10, 1),
        normal_execution_end_et=time(10, 20),
        execution_interval_minutes=1,
        no_risk_increase_after_et=time(13, 0),
    )


def _execution_config() -> Q1ExecutionConfig:
    return Q1ExecutionConfig(
        displayed_participation=Decimal("0.10"),
        adv_participation=Decimal("0.025"),
        delay_penalty_bps=Decimal("1"),
        guard_min_bps=Decimal("20"),
        guard_max_bps=Decimal("75"),
        guard_spread_multiplier=Decimal("3"),
        quantity_precision=Decimal("0.000001"),
        price_precision=Decimal("0.0001"),
        commission_rate=Decimal("0.001"),
        commission_waiver_threshold_usd=Decimal("10"),
        commission_precision=Decimal("0.01"),
        sensitivity_bps=(Decimal("5"), Decimal("10")),
    )
