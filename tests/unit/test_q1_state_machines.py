from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading.domain.enums import OrderSide
from trading.domain.q1 import (
    OrderEventType,
    Q1ArmId,
    RiskEpisodeEventType,
    RiskSeverity,
    RiskTarget,
)
from trading.execution.order_state import (
    OrderDescriptor,
    OrderEventProvenance,
    Q1OrderClass,
    append_order_event,
    expire_orders,
    pending_orders,
    reduce_order_events,
    soft_stop_buy_cancellations,
    supersede_normal_orders,
)
from trading.risk.state_machine import (
    Q1RiskError,
    RiskCheckInput,
    RiskEngineConfig,
    RiskEpisodeProvenance,
    RiskQuote,
    evaluate_risk_check,
    plan_risk_transition,
    required_residual_quote_symbols,
    residual_targets,
)

INSTANT = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)


def _order(
    order_id: str,
    side: OrderSide,
    *,
    quantity: str = "10",
    order_class: Q1OrderClass = Q1OrderClass.NORMAL,
    arm_id: str = "Q1-DET",
    symbol: str = "QQQ",
) -> OrderDescriptor:
    return OrderDescriptor(
        order_intent_id=order_id,
        arm_id=arm_id,
        portfolio_decision_id="decision-1",
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        order_class=order_class,
        created_at=INSTANT,
        valid_until=INSTANT + timedelta(minutes=20),
    )


def _order_provenance() -> OrderEventProvenance:
    return OrderEventProvenance(
        config_manifest_hash="c" * 64,
        code_version="q1_math_core_v1",
        model_version="deterministic",
        source_manifest_hash="s" * 64,
        worker_fence_token="worker-1",
        cycle_attempt_count=1,
    )


def _created(order: OrderDescriptor):
    return append_order_event(
        order=order,
        existing_events=(),
        event_type=OrderEventType.CREATED,
        occurred_at=INSTANT,
        available_at=INSTANT,
        provenance=_order_provenance(),
        source_cycle_id="cycle-1",
    )


def _risk_config() -> RiskEngineConfig:
    return RiskEngineConfig(
        version="q1-risk-v1",
        annualization_sessions=Decimal("252"),
        soft_sigma_multiple=Decimal("2"),
        hard_sigma_multiple=Decimal("3"),
        soft_daily_floor=Decimal("0.015"),
        soft_daily_ceiling=Decimal("0.030"),
        hard_daily_floor=Decimal("0.025"),
        hard_daily_ceiling=Decimal("0.050"),
        soft_drawdown_threshold=Decimal("0.08"),
        hard_drawdown_threshold=Decimal("0.12"),
        critical_drawdown_threshold=Decimal("0.18"),
        q1_hard_gross_cap=Decimal("0.50"),
        q1_hard_soxx_weight_cap=Decimal("0.20"),
        live_mirror_semiconductor_weight_cap=Decimal("0.30"),
        release_daily_loss_soft_fraction=Decimal("0.75"),
        release_drawdown_threshold=Decimal("0.06"),
        release_consecutive_valid_checks=2,
        quantity_precision=Decimal("0.000001"),
        leveraged_symbols=frozenset({"SOXL"}),
        semiconductor_symbols=frozenset(
            {"SOXL", "SOXX", "NVDA", "TSM", "KLAC", "LRCX", "MU"}
        ),
    )


def _risk_provenance() -> RiskEpisodeProvenance:
    return RiskEpisodeProvenance(
        run_id="q1-test-run",
        config_manifest_hash="c" * 64,
        code_version="q1_math_core_v1",
        model_version="deterministic",
        source_manifest_hash="s" * 64,
        worker_fence_token="worker-1",
        cycle_attempt_count=1,
    )


def _risk_check(
    *,
    arm_id: Q1ArmId = Q1ArmId.Q1_DET,
    positions: dict[str, Decimal] | None = None,
    prices: dict[str, Decimal] | None = None,
    settled_cash: str = "0",
    open_nav: str = "1000",
    peak_nav: str = "1000",
    session_id: str = "session-1",
    reconciliation_ok: bool = True,
    critical_reconciliation: bool = False,
) -> RiskCheckInput:
    if positions is None:
        positions = {"QQQ": Decimal("10")}
    if prices is None:
        prices = {"QQQ": Decimal("100")}
    return RiskCheckInput(
        arm_id=arm_id,
        calendar_session_id=session_id,
        scheduled_at=INSTANT,
        decision_created_at=INSTANT + timedelta(seconds=1),
        positions=positions,
        settled_cash_usd=Decimal(settled_cash),
        unsettled_receivables_usd=Decimal("0"),
        quotes={
            symbol: RiskQuote(
                symbol=symbol,
                quote_id=f"quote-{symbol}",
                midpoint=price,
            )
            for symbol, price in prices.items()
        },
        session_open_nav_usd=Decimal(open_nav),
        running_peak_nav_usd=Decimal(peak_nav),
        portfolio_annualized_vol=None,
        reconciliation_ok=reconciliation_ok,
        critical_reconciliation_condition=critical_reconciliation,
        reconciliation_status=(
            "OK"
            if reconciliation_ok
            else "POSITION_OR_CASH_MISMATCH"
        ),
    )


def test_zero_intent_decision_does_not_supersede_pending_order() -> None:
    buy = _order("buy-1", OrderSide.BUY)
    created = _created(buy)

    superseded = supersede_normal_orders(
        orders=(buy,),
        events=(created,),
        replacement_orders=(),
        occurred_at=INSTANT + timedelta(minutes=1),
        available_at=INSTANT + timedelta(minutes=1),
        provenance=_order_provenance(),
        source_cycle_id="empty-decision",
    )

    assert superseded == ()
    assert pending_orders((buy,), (created,))[0].order.order_intent_id == "buy-1"


def test_new_strategic_order_supersedes_pending_normal_buy_only_for_same_arm() -> None:
    buy = _order("buy-1", OrderSide.BUY)
    other_arm_buy = _order(
        "buy-other-arm",
        OrderSide.BUY,
        arm_id="Q1-LLM",
    )
    replacement = _order(
        "replacement-sell",
        OrderSide.SELL,
        quantity="1",
        symbol="SOXX",
    )
    created = (_created(buy), _created(other_arm_buy))

    superseded = supersede_normal_orders(
        orders=(buy, other_arm_buy),
        events=created,
        replacement_orders=(replacement,),
        occurred_at=INSTANT + timedelta(minutes=1),
        available_at=INSTANT + timedelta(minutes=1),
        provenance=_order_provenance(),
        source_cycle_id="new-target",
    )

    assert [event.order_intent_id for event in superseded] == ["buy-1"]
    assert reduce_order_events(buy, (*created, *superseded)).status is (
        OrderEventType.SUPERSEDED
    )
    assert reduce_order_events(
        other_arm_buy,
        (*created, *superseded),
    ).is_pending


def test_pending_normal_sell_requires_at_least_same_symbol_remaining_quantity() -> None:
    sell = _order("sell-1", OrderSide.SELL, quantity="10")
    created = _created(sell)
    partial = append_order_event(
        order=sell,
        existing_events=(created,),
        event_type=OrderEventType.PARTIALLY_FILLED,
        quantity_delta=Decimal("3"),
        occurred_at=INSTANT + timedelta(seconds=30),
        available_at=INSTANT + timedelta(seconds=30),
        provenance=_order_provenance(),
        quote_id="quote-partial",
    )
    too_small = _order("replacement-small", OrderSide.SELL, quantity="6")
    wrong_symbol = _order(
        "replacement-wrong-symbol",
        OrderSide.SELL,
        quantity="100",
        symbol="SOXX",
    )

    preserved = supersede_normal_orders(
        orders=(sell,),
        events=(created, partial),
        replacement_orders=(too_small, wrong_symbol),
        occurred_at=INSTANT + timedelta(minutes=1),
        available_at=INSTANT + timedelta(minutes=1),
        provenance=_order_provenance(),
        source_cycle_id="less-conservative-target",
    )

    assert preserved == ()
    assert reduce_order_events(sell, (created, partial)).remaining_quantity == Decimal(
        "7"
    )

    conservative = _order("replacement-conservative", OrderSide.SELL, quantity="7")
    superseded = supersede_normal_orders(
        orders=(sell,),
        events=(created, partial),
        replacement_orders=(conservative,),
        occurred_at=INSTANT + timedelta(minutes=2),
        available_at=INSTANT + timedelta(minutes=2),
        provenance=_order_provenance(),
        source_cycle_id="more-conservative-target",
    )

    assert [event.order_intent_id for event in superseded] == ["sell-1"]
    assert superseded[0].event_type is OrderEventType.SUPERSEDED


def test_emergency_order_is_never_superseded_by_new_strategic_order() -> None:
    emergency_sell = _order(
        "emergency-sell",
        OrderSide.SELL,
        order_class=Q1OrderClass.EMERGENCY_REDUCTION,
    )
    replacement = _order("replacement-buy", OrderSide.BUY)
    created = _created(emergency_sell)

    superseded = supersede_normal_orders(
        orders=(emergency_sell,),
        events=(created,),
        replacement_orders=(replacement,),
        occurred_at=INSTANT + timedelta(minutes=1),
        available_at=INSTANT + timedelta(minutes=1),
        provenance=_order_provenance(),
        source_cycle_id="new-target",
    )

    assert superseded == ()
    assert reduce_order_events(emergency_sell, (created,)).is_pending


def test_soft_stop_cancels_pending_buy_but_preserves_pending_sell() -> None:
    buy = _order("buy-1", OrderSide.BUY)
    sell = _order("sell-1", OrderSide.SELL)
    created = (_created(buy), _created(sell))

    cancellations = soft_stop_buy_cancellations(
        orders=(buy, sell),
        events=created,
        occurred_at=INSTANT + timedelta(minutes=1),
        available_at=INSTANT + timedelta(minutes=1),
        provenance=_order_provenance(),
        source_cycle_id="soft-stop",
    )
    aggregate_buy = reduce_order_events(buy, (*created, *cancellations))
    aggregate_sell = reduce_order_events(sell, (*created, *cancellations))

    assert [event.order_intent_id for event in cancellations] == ["buy-1"]
    assert aggregate_buy.status is OrderEventType.CANCELED_BY_RISK
    assert aggregate_sell.is_pending is True


def test_partial_fills_preserve_exact_remaining_and_commission_state() -> None:
    order = _order("buy-partial", OrderSide.BUY)
    created = _created(order)
    partial = append_order_event(
        order=order,
        existing_events=(created,),
        event_type=OrderEventType.PARTIALLY_FILLED,
        quantity_delta=Decimal("3.25"),
        commission_delta_usd=Decimal("0.11"),
        occurred_at=INSTANT + timedelta(minutes=1),
        available_at=INSTANT + timedelta(minutes=1),
        provenance=_order_provenance(),
        quote_id="quote-1",
    )
    filled = append_order_event(
        order=order,
        existing_events=(created, partial),
        event_type=OrderEventType.FILLED,
        quantity_delta=Decimal("6.75"),
        commission_delta_usd=Decimal("0.24"),
        occurred_at=INSTANT + timedelta(minutes=2),
        available_at=INSTANT + timedelta(minutes=2),
        provenance=_order_provenance(),
        quote_id="quote-2",
    )
    aggregate = reduce_order_events(order, (created, partial, filled))

    assert partial.remaining_quantity == Decimal("6.75")
    assert aggregate.remaining_quantity == 0
    assert aggregate.cumulative_filled_quantity == Decimal("10")
    assert aggregate.cumulative_commission_usd == Decimal("0.35")
    assert aggregate.status is OrderEventType.FILLED


def test_order_expiry_preserves_inclusive_final_slice_then_terminates() -> None:
    order = _order("expiring", OrderSide.SELL)
    created = _created(order)
    as_of = order.valid_until

    final_slice = expire_orders(
        orders=(order,),
        events=(created,),
        as_of=as_of,
        provenance=_order_provenance(),
        source_cycle_id="expiry-cycle",
    )
    expired = expire_orders(
        orders=(order,),
        events=(created,),
        as_of=as_of,
        provenance=_order_provenance(),
        source_cycle_id="expiry-cycle",
        expire_at_boundary=True,
    )

    assert final_slice == ()
    assert expired[0].event_type is OrderEventType.EXPIRED
    assert reduce_order_events(order, (created, *expired)).is_terminal is True


def test_blocked_price_guard_remains_pending_for_retry() -> None:
    order = _order("guarded", OrderSide.BUY)
    created = _created(order)
    blocked = append_order_event(
        order=order,
        existing_events=(created,),
        event_type=OrderEventType.BLOCKED_BY_PRICE_GUARD,
        occurred_at=INSTANT + timedelta(minutes=1),
        available_at=INSTANT + timedelta(minutes=1),
        provenance=_order_provenance(),
        reason="OUTSIDE_DYNAMIC_GUARD",
    )

    aggregate = reduce_order_events(order, (created, blocked))
    assert aggregate.status is OrderEventType.BLOCKED_BY_PRICE_GUARD
    assert aggregate.is_pending is True
    assert aggregate.remaining_quantity == order.quantity


def test_same_event_preparation_is_deterministic() -> None:
    order = _order("retry", OrderSide.SELL)
    created_one = _created(order)
    created_two = _created(order)

    assert created_one.event_id == created_two.event_id
    assert created_one.event_hash == created_two.event_hash
    assert created_one.idempotency_key == created_two.idempotency_key


def test_normal_to_soft_stop_is_effect_only_without_forced_targets() -> None:
    check = _risk_check(
        positions={"QQQ": Decimal("9.8")},
        prices={"QQQ": Decimal("100")},
    )
    metrics = evaluate_risk_check(check, _risk_config())
    transition = plan_risk_transition(
        check=check,
        metrics=metrics,
        config=_risk_config(),
        provenance=_risk_provenance(),
        active_episode=None,
    )

    assert metrics.indicated_severity is RiskSeverity.SOFT_STOP
    assert transition.block_new_buys is True
    assert transition.cancel_pending_buys is True
    assert transition.new_episode is None
    assert transition.executable_residual_targets == ()


def test_empty_soft_effect_does_not_block_later_hard_targets() -> None:
    soft_check = _risk_check(
        positions={"QQQ": Decimal("9.8")},
        prices={"QQQ": Decimal("100")},
    )
    soft_metrics = evaluate_risk_check(soft_check, _risk_config())
    soft = plan_risk_transition(
        check=soft_check,
        metrics=soft_metrics,
        config=_risk_config(),
        provenance=_risk_provenance(),
        active_episode=None,
    )
    hard_check = _risk_check(
        positions={"QQQ": Decimal("7"), "SOXX": Decimal("2")},
        prices={"QQQ": Decimal("100"), "SOXX": Decimal("100")},
        open_nav="1000",
    )
    hard_metrics = evaluate_risk_check(hard_check, _risk_config())
    hard = plan_risk_transition(
        check=hard_check,
        metrics=hard_metrics,
        config=_risk_config(),
        provenance=_risk_provenance(),
        active_episode=soft.active_episode,
    )

    assert hard_metrics.indicated_severity is RiskSeverity.HARD_REDUCE
    assert hard.new_episode is not None
    assert hard.new_episode.targets
    assert hard.new_events[0].event_type is RiskEpisodeEventType.ACTIVATE


def test_hard_reduce_targets_are_latched_and_not_repeatedly_halved() -> None:
    check = _risk_check(
        positions={"QQQ": Decimal("7"), "SOXX": Decimal("2")},
        prices={"QQQ": Decimal("100"), "SOXX": Decimal("100")},
    )
    metrics = evaluate_risk_check(check, _risk_config())
    first = plan_risk_transition(
        check=check,
        metrics=metrics,
        config=_risk_config(),
        provenance=_risk_provenance(),
        active_episode=None,
        source_cycle_id="first",
    )
    assert first.new_episode is not None

    repeated = plan_risk_transition(
        check=check,
        metrics=metrics,
        config=_risk_config(),
        provenance=_risk_provenance(),
        active_episode=first.new_episode,
        existing_episode_events=first.new_events,
        source_cycle_id="repeat",
    )

    assert repeated.new_episode is None
    assert repeated.new_events == ()
    assert repeated.executable_residual_targets == first.executable_residual_targets


def test_achieved_target_requires_no_quote_and_other_reduction_continues() -> None:
    targets = (
        RiskTarget(
            symbol="SOXL",
            target_quantity=Decimal("0"),
            trigger_quote_id="quote-soxl",
        ),
        RiskTarget(
            symbol="QQQ",
            target_quantity=Decimal("2"),
            trigger_quote_id="quote-qqq",
        ),
    )
    positions = {"SOXL": Decimal("0"), "QQQ": Decimal("4")}

    residual = residual_targets(targets, positions)
    required = required_residual_quote_symbols(targets, positions)

    assert [target.symbol for target in residual] == ["QQQ"]
    assert required == frozenset({"QQQ"})


def test_hard_reduce_can_escalate_to_critical_but_not_downgrade() -> None:
    hard_check = _risk_check(
        positions={"QQQ": Decimal("7"), "SOXX": Decimal("2")},
        prices={"QQQ": Decimal("100"), "SOXX": Decimal("100")},
    )
    hard_metrics = evaluate_risk_check(hard_check, _risk_config())
    hard = plan_risk_transition(
        check=hard_check,
        metrics=hard_metrics,
        config=_risk_config(),
        provenance=_risk_provenance(),
        active_episode=None,
    )
    assert hard.new_episode is not None
    critical_check = _risk_check(
        positions={"QQQ": Decimal("6"), "SOXX": Decimal("1.5")},
        prices={"QQQ": Decimal("100"), "SOXX": Decimal("100")},
        peak_nav="1000",
    )
    critical_metrics = evaluate_risk_check(critical_check, _risk_config())
    critical = plan_risk_transition(
        check=critical_check,
        metrics=critical_metrics,
        config=_risk_config(),
        provenance=_risk_provenance(),
        active_episode=hard.new_episode,
        existing_episode_events=hard.new_events,
    )

    assert critical_metrics.indicated_severity is RiskSeverity.CRITICAL_EXIT
    assert critical.effective_severity is RiskSeverity.CRITICAL_EXIT
    assert critical.new_events[0].event_type is RiskEpisodeEventType.ESCALATE
    assert all(target.target_quantity == 0 for target in critical.new_events[0].targets)

    with pytest.raises(Q1RiskError, match="downgrade"):
        downgraded = critical.new_events[0].model_copy(
            update={
                "risk_episode_event_id": "bad",
                "event_type": RiskEpisodeEventType.TARGET_PROGRESS,
                "event_sequence": 3,
                "severity": RiskSeverity.SOFT_STOP,
                "targets": (),
                "target_symbol": "QQQ",
                "observed_quantity": Decimal("1"),
                "residual_quantity": Decimal("1"),
                "idempotency_key": "bad",
            }
        )
        plan_risk_transition(
            check=critical_check,
            metrics=critical_metrics,
            config=_risk_config(),
            provenance=_risk_provenance(),
            active_episode=hard.new_episode,
            existing_episode_events=(
                *hard.new_events,
                critical.new_events[0],
                downgraded,
            ),
        )


def test_release_requires_next_session_two_checks_and_never_auto_buys() -> None:
    hard_check = _risk_check(
        positions={"QQQ": Decimal("7"), "SOXX": Decimal("2")},
        prices={"QQQ": Decimal("100"), "SOXX": Decimal("100")},
    )
    hard_metrics = evaluate_risk_check(hard_check, _risk_config())
    hard = plan_risk_transition(
        check=hard_check,
        metrics=hard_metrics,
        config=_risk_config(),
        provenance=_risk_provenance(),
        active_episode=None,
    )
    assert hard.new_episode is not None
    recovered_check = _risk_check(
        positions={"QQQ": Decimal("4"), "SOXX": Decimal("1")},
        prices={"QQQ": Decimal("100"), "SOXX": Decimal("100")},
        settled_cash="500",
        open_nav="1000",
        peak_nav="1050",
        session_id="session-2",
    )
    recovered_metrics = evaluate_risk_check(recovered_check, _risk_config())

    one_check = plan_risk_transition(
        check=recovered_check,
        metrics=recovered_metrics,
        config=_risk_config(),
        provenance=_risk_provenance(),
        active_episode=hard.new_episode,
        existing_episode_events=hard.new_events,
        is_next_session_strategic_cycle=True,
        consecutive_valid_release_checks=1,
    )
    released = plan_risk_transition(
        check=recovered_check,
        metrics=recovered_metrics,
        config=_risk_config(),
        provenance=_risk_provenance(),
        active_episode=hard.new_episode,
        existing_episode_events=hard.new_events,
        is_next_session_strategic_cycle=True,
        consecutive_valid_release_checks=2,
    )

    assert one_check.effective_severity is RiskSeverity.HARD_REDUCE
    assert released.new_events[-1].event_type is RiskEpisodeEventType.RELEASE
    assert released.effective_severity is RiskSeverity.NORMAL
    assert released.release_allows_automatic_buys is False


def test_live_mirror_soxl_loss_check_needs_no_qqq_data() -> None:
    check = _risk_check(
        arm_id=Q1ArmId.LIVE_MIRROR,
        positions={"SOXL": Decimal("8.3")},
        prices={"SOXL": Decimal("100")},
        open_nav="1000",
    )
    metrics = evaluate_risk_check(check, _risk_config())
    transition = plan_risk_transition(
        check=check,
        metrics=metrics,
        config=_risk_config(),
        provenance=_risk_provenance(),
        active_episode=None,
    )

    assert metrics.indicated_severity is RiskSeverity.HARD_REDUCE
    assert transition.new_episode is not None
    assert transition.new_episode.targets[0].symbol == "SOXL"
    assert transition.new_episode.targets[0].target_quantity == 0
    assert transition.required_quote_symbols == frozenset({"SOXL"})


def test_empty_hard_targets_never_create_active_latch() -> None:
    check = _risk_check(
        positions={},
        prices={},
        settled_cash="830",
        open_nav="1000",
    )
    metrics = evaluate_risk_check(check, _risk_config())
    transition = plan_risk_transition(
        check=check,
        metrics=metrics,
        config=_risk_config(),
        provenance=_risk_provenance(),
        active_episode=None,
    )

    assert metrics.indicated_severity is RiskSeverity.HARD_REDUCE
    assert transition.new_episode is None
    assert transition.active_episode is None
    assert transition.executable_residual_targets == ()


def test_risk_check_rejects_only_missing_quotes_for_held_assets() -> None:
    check = _risk_check(
        arm_id=Q1ArmId.LIVE_MIRROR,
        positions={"SOXL": Decimal("1")},
        prices={},
    )
    with pytest.raises(Q1RiskError, match="SOXL"):
        evaluate_risk_check(check, _risk_config())


def test_test_clock_is_stable() -> None:
    assert INSTANT.date() == date(2026, 7, 27)
