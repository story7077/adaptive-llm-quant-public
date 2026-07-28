from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading.experiments.arms import ArmState
from trading.llm.policy_compiler import PolicyState
from trading.portfolio.forward import (
    apply_core_rebalance_band,
    build_core_forecast,
    target_for_arm,
)
from trading.risk.forward import (
    DecisionQuote,
    ForwardRiskConfig,
    build_forced_reduction_targets,
    build_forward_order_plan,
    evaluate_forward_loss_guard,
    resolve_forced_reduction_targets,
)


def _core():
    closes = [100.0 * (1.002**index) * (1.01 if index % 2 else 0.99) for index in range(21)]
    return build_core_forecast(
        closes,
        version="b0_forward_core_v1",
        lookback_sessions=20,
        target_annualized_vol=0.12,
    )


def _quote(symbol: str, price: str = "100") -> DecisionQuote:
    instant = datetime(2026, 7, 28, 19, 44, 59, tzinfo=UTC)
    mid = Decimal(price)
    return DecisionQuote(
        symbol=symbol,
        quote_id=f"quote-{symbol}",
        bid_price=mid - Decimal("0.05"),
        ask_price=mid + Decimal("0.05"),
        bid_size_round_lots=10,
        ask_size_round_lots=10,
        event_time=instant,
        available_at=instant,
    )


def _config() -> ForwardRiskConfig:
    return ForwardRiskConfig(
        version="risk_v2",
        transition_version="inherited_account_transition_v1",
        core_version="b0_forward_core_v1",
        max_one_way_daily_turnover=Decimal("0.25"),
        min_order_notional_usd=Decimal("1"),
        buy_cash_reserve_fraction=Decimal("0.01"),
        spend_unsettled_sale_proceeds=False,
        max_single_symbol_weight=Decimal("0.35"),
        max_semiconductor_cluster_weight=Decimal("0.55"),
        max_leveraged_etf_weight=Decimal("0.15"),
        max_combined_leveraged_etf_weight=Decimal("0.20"),
        max_gross_exposure=Decimal("1"),
        soft_daily_loss_fraction=Decimal("0.015"),
        hard_daily_loss_fraction=Decimal("0.025"),
        block_entries_drawdown_fraction=Decimal("0.08"),
        force_reduce_drawdown_fraction=Decimal("0.12"),
        force_reduce_core_fraction=Decimal("0.50"),
        commission_rate=Decimal("0.001"),
        commission_waiver_threshold_usd=Decimal("10"),
        delay_penalty_bps=Decimal("1"),
        quantity_precision=Decimal("0.000001"),
        sell_only_symbols=frozenset({"SOXL", "NVDA"}),
        entry_symbols=frozenset({"QQQ", "SOXS", "SOXX"}),
        semiconductor_symbols=frozenset(
            {"SOXL", "SOXS", "SOXX", "NVDA"}
        ),
        leveraged_symbols=frozenset({"SOXL", "SOXS"}),
        core_cap_exempt_arms=frozenset({"B0-QQQ", "B0-VOL", "B3-RISK"}),
    )


def test_b0_vol_is_a_versioned_non_alpha_core() -> None:
    core = _core()
    target = target_for_arm("B0-VOL", core=core)

    assert core.observations == 20
    assert 0 < core.qqq_weight <= 1
    assert target.target_weights["QQQ"] + target.target_weights["USD_CASH"] == 1
    assert target.target_reason == "VOL_TARGETED_QQQ_CASH_CORE"


def test_transition_sells_leverage_first_and_does_not_spend_sale_proceeds() -> None:
    core = _core()
    state = ArmState(
        arm_id="B0-QQQ",
        initial_cash_usd=Decimal("3000"),
        cash_usd=Decimal("1000"),
        positions={"SOXL": Decimal("10"), "NVDA": Decimal("10")},
        sequence=0,
    )
    quotes = {symbol: _quote(symbol) for symbol in ("QQQ", "SOXL", "NVDA")}
    plan = build_forward_order_plan(
        run_id="paper-test",
        cycle_id="decision-cycle",
        state=state,
        target=target_for_arm("B0-QQQ", core=core),
        quotes=quotes,
        decision_time=datetime(2026, 7, 28, 19, 45, tzinfo=UTC),
        intent_created_at=datetime(2026, 7, 28, 19, 45, 2, tzinfo=UTC),
        valid_until=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
        input_snapshot_hash="a" * 64,
        session_open_nav_usd=Decimal("3000"),
        today_buy_notional_usd=Decimal("0"),
        today_sell_notional_usd=Decimal("0"),
        unsettled_sale_proceeds_usd=Decimal("0"),
        config=_config(),
    )

    assert plan.risk_decision.approved is True
    assert plan.intents
    sells = [item for item in plan.intents if item.side.value == "SELL"]
    buys = [item for item in plan.intents if item.side.value == "BUY"]
    assert sells[0].symbol == "SOXL"
    assert all(item.symbol not in {"SOXL", "NVDA"} for item in buys)
    assert buys[0].symbol == "QQQ"
    buy_estimate = buys[0].quantity * quotes["QQQ"].ask_price
    assert buy_estimate < state.cash_usd


def test_b3_risk_block_prevents_new_qqq_entry_but_still_allows_soxl_reduction() -> None:
    core = _core()
    policy = PolicyState(
        arm_id="B3-RISK",
        version=3,
        portfolio_risk_multiplier=0.5,
        strategy_risk_deltas={},
        blocked_targets=frozenset({"SYMBOL:QQQ"}),
        active_buckets=frozenset({"SYMBOL:QQQ"}),
        source_patch_id="patch-3",
    )
    state = ArmState(
        arm_id="B3-RISK",
        initial_cash_usd=Decimal("3000"),
        cash_usd=Decimal("1000"),
        positions={"SOXL": Decimal("20")},
        sequence=4,
    )
    plan = build_forward_order_plan(
        run_id="paper-test",
        cycle_id="decision-cycle",
        state=state,
        target=target_for_arm("B3-RISK", core=core, policy=policy),
        quotes={"QQQ": _quote("QQQ"), "SOXL": _quote("SOXL")},
        decision_time=datetime(2026, 7, 28, 19, 45, tzinfo=UTC),
        intent_created_at=datetime(2026, 7, 28, 19, 45, 2, tzinfo=UTC),
        valid_until=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
        input_snapshot_hash="b" * 64,
        session_open_nav_usd=Decimal("3000"),
        today_buy_notional_usd=Decimal("0"),
        today_sell_notional_usd=Decimal("0"),
        unsettled_sale_proceeds_usd=Decimal("0"),
        config=_config(),
    )

    assert all(item.side.value == "SELL" for item in plan.intents)
    assert {item.symbol for item in plan.intents} == {"SOXL"}


def test_arm_state_rejects_short_and_negative_cash() -> None:
    from trading.domain.contracts import Fill
    from trading.domain.enums import OrderSide

    instant = datetime(2026, 7, 28, 19, 46, tzinfo=UTC)
    sell_too_much = Fill(
        fill_id="fill-sell",
        order_intent_id="order-sell",
        arm_id="B0-CASH",
        symbol="SOXL",
        side=OrderSide.SELL,
        quantity=Decimal("2"),
        price=Decimal("100"),
        commission_usd=Decimal("0.20"),
        execution_scenario_id="test",
        effective_at=instant,
        created_at=instant,
    )
    buy_too_much = sell_too_much.model_copy(
        update={
            "fill_id": "fill-buy",
            "order_intent_id": "order-buy",
            "symbol": "QQQ",
            "side": OrderSide.BUY,
            "quantity": Decimal("20"),
        }
    )
    state = ArmState(
        arm_id="B0-CASH",
        initial_cash_usd=Decimal("1000"),
        cash_usd=Decimal("1000"),
        positions={"SOXL": Decimal("1")},
        sequence=0,
    )

    import pytest

    with pytest.raises(ValueError, match="short"):
        state.apply_fill(sell_too_much)
    with pytest.raises(ValueError, match="negative USD cash"):
        state.apply_fill(buy_too_much)


def test_core_rebalance_band_holds_small_vol_target_change() -> None:
    core = _core()
    state = ArmState(
        arm_id="B0-VOL",
        initial_cash_usd=Decimal("1000"),
        cash_usd=Decimal("500"),
        positions={"QQQ": Decimal("5")},
        sequence=2,
    )
    target = target_for_arm("B0-VOL", core=core)
    target = target.__class__(
        arm_id=target.arm_id,
        target_weights={"QQQ": 0.53, "USD_CASH": 0.47},
        core_forecast=target.core_forecast,
        policy_version=target.policy_version,
        policy_risk_multiplier=target.policy_risk_multiplier,
        blocked_new_entries=target.blocked_new_entries,
        target_reason=target.target_reason,
    )

    banded = apply_core_rebalance_band(
        target,
        cash_usd=state.cash_usd,
        positions=state.positions,
        quotes={"QQQ": _quote("QQQ")},
        min_weight_delta=0.05,
    )

    assert banded.target_weights == {"QQQ": 0.5, "USD_CASH": 0.5}
    assert banded.target_reason.endswith("WITHIN_REBALANCE_BAND")


def test_loss_guard_boundaries_and_forced_reduction_budget() -> None:
    state = ArmState(
        arm_id="B3-RISK",
        initial_cash_usd=Decimal("1000"),
        cash_usd=Decimal("100"),
        positions={"SOXL": Decimal("2"), "NVDA": Decimal("5")},
        sequence=0,
    )
    quotes = {
        "SOXL": _quote("SOXL", "100"),
        "NVDA": _quote("NVDA", "100"),
    }

    soft = evaluate_forward_loss_guard(
        state=state,
        quotes=quotes,
        session_open_nav_usd=Decimal("815"),
        peak_nav_usd=Decimal("800"),
        config=_config(),
    )
    assert soft.state == "SOFT_STOP"
    assert soft.block_new_entries is True

    forced = evaluate_forward_loss_guard(
        state=state,
        quotes=quotes,
        session_open_nav_usd=Decimal("800"),
        peak_nav_usd=Decimal("920"),
        config=_config(),
    )
    assert forced.state == "FORCE_REDUCE"
    assert forced.forced_sell_budget_usd is not None
    assert forced.forced_sell_budget_usd >= Decimal("199")
    created_targets = resolve_forced_reduction_targets(
        guard=forced,
        latched_targets=None,
        state=state,
        quotes=quotes,
        config=_config(),
        loss_controls_applied=True,
    )
    assert created_targets is not None
    assert created_targets["SOXL"] == 0

    drawdown_block_only = evaluate_forward_loss_guard(
        state=state,
        quotes=quotes,
        session_open_nav_usd=Decimal("800"),
        peak_nav_usd=Decimal("870"),
        config=_config(),
    )
    assert drawdown_block_only.state == "HARD_STOP"
    assert drawdown_block_only.block_new_entries is True
    assert drawdown_block_only.forced_sell_budget_usd is None
    assert (
        resolve_forced_reduction_targets(
            guard=drawdown_block_only,
            latched_targets=None,
            state=state,
            quotes=quotes,
            config=_config(),
            loss_controls_applied=True,
        )
        is None
    )
    latched = {"SOXL": Decimal("0"), "NVDA": Decimal("4")}
    assert resolve_forced_reduction_targets(
        guard=drawdown_block_only,
        latched_targets=latched,
        state=state,
        quotes=quotes,
        config=_config(),
        loss_controls_applied=True,
    ) == latched


def test_expected_cost_includes_spread_delay_and_commission() -> None:
    core = _core()
    state = ArmState(
        arm_id="B0-QQQ",
        initial_cash_usd=Decimal("1000"),
        cash_usd=Decimal("1000"),
        positions={},
        sequence=0,
    )
    plan = build_forward_order_plan(
        run_id="paper-test",
        cycle_id="decision-cost",
        state=state,
        target=target_for_arm("B0-QQQ", core=core),
        quotes={"QQQ": _quote("QQQ")},
        decision_time=datetime(2026, 7, 28, 19, 45, tzinfo=UTC),
        intent_created_at=datetime(2026, 7, 28, 19, 45, 2, tzinfo=UTC),
        valid_until=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
        input_snapshot_hash="c" * 64,
        session_open_nav_usd=Decimal("1000"),
        today_buy_notional_usd=Decimal("0"),
        today_sell_notional_usd=Decimal("0"),
        unsettled_sale_proceeds_usd=Decimal("0"),
        config=_config(),
    )

    commission_only = sum(
        (
            intent.quantity
            * Decimal("100.06")
            * _config().commission_rate
            for intent in plan.intents
        ),
        Decimal("0"),
    )
    assert plan.portfolio_decision.expected_cost_usd > commission_only


def test_hard_daily_loss_creates_a_reduce_only_core_sell() -> None:
    core = _core()
    state = ArmState(
        arm_id="B3-RISK",
        initial_cash_usd=Decimal("1000"),
        cash_usd=Decimal("0"),
        positions={"QQQ": Decimal("9.75")},
        sequence=3,
    )
    quotes = {"QQQ": _quote("QQQ")}
    guard = evaluate_forward_loss_guard(
        state=state,
        quotes=quotes,
        session_open_nav_usd=Decimal("1000"),
        peak_nav_usd=Decimal("975"),
        config=_config(),
    )
    assert guard.state == "HARD_STOP"
    assert guard.forced_sell_budget_usd == Decimal("487.5000")

    plan = build_forward_order_plan(
        run_id="paper-test",
        cycle_id="hard-loss-cycle",
        state=state,
        target=target_for_arm("B3-RISK", core=core),
        quotes=quotes,
        decision_time=datetime(2026, 7, 28, 19, 45, tzinfo=UTC),
        intent_created_at=datetime(2026, 7, 28, 19, 45, 1, tzinfo=UTC),
        valid_until=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
        input_snapshot_hash="d" * 64,
        session_open_nav_usd=guard.session_open_nav_usd,
        today_buy_notional_usd=Decimal("0"),
        today_sell_notional_usd=Decimal("0"),
        unsettled_sale_proceeds_usd=Decimal("0"),
        config=_config(),
        loss_state=guard.state,
        block_new_entries=True,
        forced_target_quantities=build_forced_reduction_targets(
            state=state,
            quotes=quotes,
            current_nav=guard.current_nav_usd,
            config=_config(),
        ),
        allow_transition_sells=False,
    )

    assert plan.intents
    assert all(intent.side.value == "SELL" for intent in plan.intents)
    assert {intent.symbol for intent in plan.intents} == {"QQQ"}


def test_hard_loss_reuses_fixed_target_and_never_substitutes_sgov() -> None:
    core = _core()
    quotes = {
        "QQQ": _quote("QQQ"),
        "SGOV": _quote("SGOV"),
    }
    fixed_targets = {"QQQ": Decimal("2.500000")}
    state = ArmState(
        arm_id="B3-RISK",
        initial_cash_usd=Decimal("975"),
        cash_usd=Decimal("0"),
        positions={"QQQ": Decimal("5"), "SGOV": Decimal("4.75")},
        sequence=8,
    )
    first = build_forward_order_plan(
        run_id="paper-test",
        cycle_id="hard-loss-fixed-1",
        state=state,
        target=target_for_arm("B3-RISK", core=core),
        quotes=quotes,
        decision_time=datetime(2026, 7, 28, 19, 45, tzinfo=UTC),
        intent_created_at=datetime(2026, 7, 28, 19, 45, 1, tzinfo=UTC),
        valid_until=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
        input_snapshot_hash="e" * 64,
        session_open_nav_usd=Decimal("1000"),
        today_buy_notional_usd=Decimal("0"),
        today_sell_notional_usd=Decimal("0"),
        unsettled_sale_proceeds_usd=Decimal("0"),
        config=_config(),
        loss_state="HARD_STOP",
        block_new_entries=True,
        forced_target_quantities=fixed_targets,
        allow_transition_sells=False,
    )

    assert {intent.symbol for intent in first.intents} == {"QQQ"}
    assert first.intents[0].quantity == Decimal("2.500000")
    assert (
        first.risk_decision.forced_reduction_actions[0]["target_quantities"]
        == {"QQQ": "2.500000"}
    )

    reached = state.__class__(
        arm_id=state.arm_id,
        initial_cash_usd=state.initial_cash_usd,
        cash_usd=Decimal("249"),
        positions={"QQQ": Decimal("2.5"), "SGOV": Decimal("4.75")},
        sequence=9,
    )
    replay = build_forward_order_plan(
        run_id="paper-test",
        cycle_id="hard-loss-fixed-2",
        state=reached,
        target=target_for_arm("B3-RISK", core=core),
        quotes=quotes,
        decision_time=datetime(2026, 7, 28, 19, 50, tzinfo=UTC),
        intent_created_at=datetime(2026, 7, 28, 19, 50, 1, tzinfo=UTC),
        valid_until=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
        input_snapshot_hash="f" * 64,
        session_open_nav_usd=Decimal("1000"),
        today_buy_notional_usd=Decimal("0"),
        today_sell_notional_usd=Decimal("250"),
        unsettled_sale_proceeds_usd=Decimal("249"),
        config=_config(),
        loss_state="HARD_STOP",
        block_new_entries=True,
        forced_target_quantities=fixed_targets,
        allow_transition_sells=False,
    )

    assert replay.intents == ()
    assert Decimal(replay.diagnostics["forced_sell_residual_usd"]) == 0


def test_forced_reduction_orders_semiconductor_before_qqq_core() -> None:
    state = ArmState(
        arm_id="B3-RISK",
        initial_cash_usd=Decimal("1000"),
        cash_usd=Decimal("0"),
        positions={"QQQ": Decimal("3"), "SOXX": Decimal("7")},
        sequence=5,
    )
    quotes = {"QQQ": _quote("QQQ"), "SOXX": _quote("SOXX")}
    plan = build_forward_order_plan(
        run_id="paper-test",
        cycle_id="forced-priority",
        state=state,
        target=target_for_arm("B3-RISK", core=_core()),
        quotes=quotes,
        decision_time=datetime(2026, 7, 28, 19, 45, tzinfo=UTC),
        intent_created_at=datetime(2026, 7, 28, 19, 45, 1, tzinfo=UTC),
        valid_until=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
        input_snapshot_hash="2" * 64,
        session_open_nav_usd=Decimal("1000"),
        today_buy_notional_usd=Decimal("0"),
        today_sell_notional_usd=Decimal("0"),
        unsettled_sale_proceeds_usd=Decimal("0"),
        config=_config(),
        loss_state="FORCE_REDUCE",
        block_new_entries=True,
        forced_target_quantities={
            "SOXX": Decimal("5.5"),
            "QQQ": Decimal("1.5"),
        },
        allow_transition_sells=False,
    )

    assert [intent.symbol for intent in plan.intents] == ["SOXX", "QQQ"]


def test_unsettled_sale_proceeds_are_not_available_for_same_day_buys() -> None:
    state = ArmState(
        arm_id="B0-QQQ",
        initial_cash_usd=Decimal("500"),
        cash_usd=Decimal("500"),
        positions={},
        sequence=2,
    )
    plan = build_forward_order_plan(
        run_id="paper-test",
        cycle_id="unsettled-cash",
        state=state,
        target=target_for_arm("B0-QQQ", core=_core()),
        quotes={"QQQ": _quote("QQQ")},
        decision_time=datetime(2026, 7, 28, 19, 45, tzinfo=UTC),
        intent_created_at=datetime(2026, 7, 28, 19, 45, 1, tzinfo=UTC),
        valid_until=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
        input_snapshot_hash="1" * 64,
        session_open_nav_usd=Decimal("500"),
        today_buy_notional_usd=Decimal("0"),
        today_sell_notional_usd=Decimal("500"),
        unsettled_sale_proceeds_usd=Decimal("500"),
        config=_config(),
    )

    assert plan.intents == ()
    assert plan.diagnostics["unsettled_sale_proceeds_usd"] == "500"
