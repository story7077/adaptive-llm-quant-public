from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading.research.shadow import ShadowExecutionContract
from trading.research.shadow_runtime import (
    MatchedShadowCycleResultV1,
    ShadowArmRole,
    ShadowPaperParametersV1,
    ShadowQuoteV1,
    ShadowStrategyBindingV1,
    build_initial_shadow_state,
    build_matched_quote_bundle,
    build_shadow_pair_runtime_spec,
    build_shadow_target_decision,
    execute_matched_shadow_cycle,
    summarize_matched_shadow_results,
)

NOW = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
MARKET_HASH = "a" * 64


def _parameters() -> ShadowPaperParametersV1:
    return ShadowPaperParametersV1(
        contract_version="shadow-paper-v1",
        commission_rate=Decimal("0.001"),
        commission_waiver_threshold_usd=Decimal("0"),
        delay_penalty_bps=Decimal("1"),
        displayed_participation_rate=Decimal("0.10"),
        adv_participation_rate=Decimal("0.025"),
        minimum_order_notional_usd=Decimal("25"),
        quantity_quantum=Decimal("0.000001"),
        price_quantum=Decimal("0.0001"),
        sensitivity_5_bps=Decimal("5"),
        sensitivity_10_bps=Decimal("10"),
        basis_points_per_unit_return=Decimal("10000"),
        maximum_quote_age_seconds=15,
        weight_tolerance=Decimal("0.000001"),
        real_order_routing=False,
    )


def _spec():
    return build_shadow_pair_runtime_spec(
        shadow_pair_id="pair-1",
        challenger_id="challenger-1",
        champion=ShadowStrategyBindingV1(
            role=ShadowArmRole.CHAMPION,
            arm_id="champion-arm",
            strategy_id="T1",
            strategy_version="1.0.0",
            artifact_hash="b" * 64,
        ),
        challenger=ShadowStrategyBindingV1(
            role=ShadowArmRole.CHALLENGER,
            arm_id="challenger-arm",
            strategy_id="T1",
            strategy_version="1.1.0",
            artifact_hash="c" * 64,
        ),
        execution_contract=ShadowExecutionContract(
            market_input_manifest_hash=MARKET_HASH,
            decision_schedule_version="schedule-v1",
            execution_scenario_version="execution-v1",
            cost_model_version="cost-v1",
            starting_capital_usd="100000.00",
            liquidity_policy_version="liquidity-v1",
        ),
        paper_parameters=_parameters(),
        code_version="code-v1",
        created_at=NOW,
    )


def _quotes(*, size: Decimal = Decimal("10000")):
    event_time = NOW + timedelta(minutes=1)
    return build_matched_quote_bundle(
        market_input_manifest_hash=MARKET_HASH,
        as_of=event_time + timedelta(seconds=1),
        quotes=(
            ShadowQuoteV1(
                quote_id="quote-qqq",
                instrument_id="QQQ",
                event_time=event_time,
                available_at=event_time,
                bid_price=Decimal("199"),
                ask_price=Decimal("201"),
                bid_size_shares=size,
                ask_size_shares=size,
                adv_shares=Decimal("1000000"),
                source_hash="d" * 64,
            ),
            ShadowQuoteV1(
                quote_id="quote-spy",
                instrument_id="SPY",
                event_time=event_time,
                available_at=event_time,
                bid_price=Decimal("99"),
                ask_price=Decimal("101"),
                bid_size_shares=size,
                ask_size_shares=size,
                adv_shares=Decimal("1000000"),
                source_hash="e" * 64,
            ),
        ),
    )


def _targets(spec, quote_manifest_hash: str):
    common = {
        "spec": spec,
        "decision_time": NOW,
        "signal_data_cutoff": NOW - timedelta(minutes=1),
        "valid_until": NOW + timedelta(minutes=20),
        "quote_manifest_hash": quote_manifest_hash,
    }
    champion = build_shadow_target_decision(
        target_id="target-champion-1",
        role=ShadowArmRole.CHAMPION,
        target_weights={
            "SPY": Decimal("0.5"),
            "USD_CASH": Decimal("0.5"),
        },
        **common,
    )
    challenger = build_shadow_target_decision(
        target_id="target-challenger-1",
        role=ShadowArmRole.CHALLENGER,
        target_weights={
            "QQQ": Decimal("0.5"),
            "USD_CASH": Decimal("0.5"),
        },
        **common,
    )
    return champion, challenger


def _execute(size: Decimal = Decimal("10000")) -> MatchedShadowCycleResultV1:
    spec = _spec()
    quotes = _quotes(size=size)
    champion_target, challenger_target = _targets(
        spec,
        quotes.quote_manifest_hash,
    )
    return execute_matched_shadow_cycle(
        spec=spec,
        champion_state=build_initial_shadow_state(
            spec=spec,
            role=ShadowArmRole.CHAMPION,
        ),
        challenger_state=build_initial_shadow_state(
            spec=spec,
            role=ShadowArmRole.CHALLENGER,
        ),
        champion_target=champion_target,
        challenger_target=challenger_target,
        quote_bundle=quotes,
    )


def test_matched_runtime_keeps_independent_long_only_paper_books() -> None:
    result = _execute()
    assert result.real_order_routing is False
    assert result.champion.next_state.arm_id != result.challenger.next_state.arm_id
    assert set(result.champion.next_state.position_map()) == {"SPY"}
    assert set(result.challenger.next_state.position_map()) == {"QQQ"}
    for arm in (result.champion, result.challenger):
        assert arm.next_state.cash_usd >= 0
        assert all(quantity > 0 for quantity in arm.next_state.position_map().values())
        assert len(arm.orders) == len(arm.fills) == len(arm.ledger_entries) == 1
        assert arm.fill_costs[0].sensitivity_10bp_cost_usd > (
            arm.fill_costs[0].sensitivity_5bp_cost_usd
        )
        assert arm.fill_costs[0].sensitivity_5bp_cost_usd > (
            arm.fill_costs[0].base_execution_cost_usd
        )
        assert arm.daily_summary.cash_weight + sum(
            arm.daily_summary.exposures.values(),
            Decimal("0"),
        ) == pytest.approx(Decimal("1"), abs=Decimal("0.000001"))


def test_conservative_liquidity_cap_partial_fills_without_chasing() -> None:
    result = _execute(size=Decimal("100"))
    for arm in (result.champion, result.challenger):
        assert arm.fills[0].quantity == Decimal("10.000000")
        assert arm.fills[0].quantity < arm.orders[0].quantity
        quote = result.quote_bundle.quote_map()[arm.fills[0].symbol]
        if arm.fills[0].side.value == "BUY":
            assert arm.fills[0].price > quote.ask_price


def test_runtime_is_deterministic_and_summary_makes_no_profitability_claim() -> None:
    first = _execute()
    second = _execute()
    assert first.result_hash == second.result_hash
    summary = summarize_matched_shadow_results(
        spec=_spec(),
        results=(first,),
        replay_hash="f" * 64,
    )
    assert summary.profitability_claimed is False
    assert summary.common_sessions == 1
    assert summary.mean_matched_daily_return_difference == (
        first.matched_daily_return_difference
    )


def test_target_binding_and_long_only_constraints_fail_closed() -> None:
    spec = _spec()
    quotes = _quotes()
    champion_target, challenger_target = _targets(
        spec,
        quotes.quote_manifest_hash,
    )
    forged = champion_target.model_copy(update={"artifact_hash": "f" * 64})
    with pytest.raises(ValueError, match="host-bound"):
        execute_matched_shadow_cycle(
            spec=spec,
            champion_state=build_initial_shadow_state(
                spec=spec,
                role=ShadowArmRole.CHAMPION,
            ),
            challenger_state=build_initial_shadow_state(
                spec=spec,
                role=ShadowArmRole.CHALLENGER,
            ),
            champion_target=forged,
            challenger_target=challenger_target,
            quote_bundle=quotes,
        )
    with pytest.raises(ValidationError):
        build_shadow_target_decision(
            target_id="short-target",
            spec=spec,
            role=ShadowArmRole.CHAMPION,
            decision_time=NOW,
            signal_data_cutoff=NOW,
            valid_until=NOW + timedelta(minutes=1),
            quote_manifest_hash=quotes.quote_manifest_hash,
            target_weights={
                "SPY": Decimal("-0.1"),
                "USD_CASH": Decimal("1.1"),
            },
        )
    with pytest.raises(ValidationError, match="sum to one"):
        build_shadow_target_decision(
            target_id="leveraged-target",
            spec=spec,
            role=ShadowArmRole.CHAMPION,
            decision_time=NOW,
            signal_data_cutoff=NOW,
            valid_until=NOW + timedelta(minutes=1),
            quote_manifest_hash=quotes.quote_manifest_hash,
            target_weights={
                "SPY": Decimal("0.8"),
                "QQQ": Decimal("0.4"),
                "USD_CASH": Decimal("0"),
            },
        )
