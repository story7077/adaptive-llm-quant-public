from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading.domain.hashing import canonical_hash
from trading.research.shadow import ShadowExecutionContract
from trading.research.shadow_runtime import (
    ShadowArmRole,
    ShadowPaperParametersV1,
    ShadowQuoteV1,
    ShadowStrategyBindingV1,
    build_initial_shadow_state,
    build_matched_quote_bundle,
    build_shadow_pair_runtime_spec,
    build_shadow_target_decision,
    execute_matched_shadow_cycle,
)

START = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
MARKET_HASH = "a" * 64


def _run_complete_sequence() -> tuple[str, ...]:
    parameters = ShadowPaperParametersV1(
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
    spec = build_shadow_pair_runtime_spec(
        shadow_pair_id="pair-replay",
        challenger_id="challenger-replay",
        champion=ShadowStrategyBindingV1(
            role=ShadowArmRole.CHAMPION,
            arm_id="champion-replay",
            strategy_id="T1",
            strategy_version="1.0.0",
            artifact_hash="b" * 64,
        ),
        challenger=ShadowStrategyBindingV1(
            role=ShadowArmRole.CHALLENGER,
            arm_id="challenger-replay",
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
        paper_parameters=parameters,
        code_version="code-v1",
        created_at=START,
    )
    champion_state = build_initial_shadow_state(
        spec=spec,
        role=ShadowArmRole.CHAMPION,
    )
    challenger_state = build_initial_shadow_state(
        spec=spec,
        role=ShadowArmRole.CHALLENGER,
    )
    result_hashes: list[str] = []
    for sequence, (champion_risk, challenger_risk) in enumerate(
        (
            (Decimal("0.5"), Decimal("0.6")),
            (Decimal("0"), Decimal("0")),
        )
    ):
        decision_time = START + timedelta(days=sequence, minutes=10)
        quote_time = decision_time + timedelta(seconds=1)
        bundle = build_matched_quote_bundle(
            market_input_manifest_hash=MARKET_HASH,
            as_of=quote_time + timedelta(seconds=1),
            quotes=(
                ShadowQuoteV1(
                    quote_id=f"quote-qqq-{sequence}",
                    instrument_id="QQQ",
                    event_time=quote_time,
                    available_at=quote_time,
                    bid_price=Decimal("199") + sequence,
                    ask_price=Decimal("201") + sequence,
                    bid_size_shares=Decimal("10000"),
                    ask_size_shares=Decimal("10000"),
                    adv_shares=Decimal("1000000"),
                    source_hash=canonical_hash(("QQQ", sequence)),
                ),
                ShadowQuoteV1(
                    quote_id=f"quote-spy-{sequence}",
                    instrument_id="SPY",
                    event_time=quote_time,
                    available_at=quote_time,
                    bid_price=Decimal("99") + sequence,
                    ask_price=Decimal("101") + sequence,
                    bid_size_shares=Decimal("10000"),
                    ask_size_shares=Decimal("10000"),
                    adv_shares=Decimal("1000000"),
                    source_hash=canonical_hash(("SPY", sequence)),
                ),
            ),
        )
        common = {
            "spec": spec,
            "decision_time": decision_time,
            "signal_data_cutoff": decision_time - timedelta(minutes=1),
            "valid_until": decision_time + timedelta(minutes=20),
            "quote_manifest_hash": bundle.quote_manifest_hash,
        }
        champion_target = build_shadow_target_decision(
            target_id=f"champion-target-{sequence}",
            role=ShadowArmRole.CHAMPION,
            target_weights={
                "SPY": champion_risk,
                "USD_CASH": Decimal("1") - champion_risk,
            },
            **common,
        )
        challenger_target = build_shadow_target_decision(
            target_id=f"challenger-target-{sequence}",
            role=ShadowArmRole.CHALLENGER,
            target_weights={
                "QQQ": challenger_risk,
                "USD_CASH": Decimal("1") - challenger_risk,
            },
            **common,
        )
        result = execute_matched_shadow_cycle(
            spec=spec,
            champion_state=champion_state,
            challenger_state=challenger_state,
            champion_target=champion_target,
            challenger_target=challenger_target,
            quote_bundle=bundle,
        )
        result_hashes.append(result.result_hash)
        champion_state = result.champion.next_state
        challenger_state = result.challenger.next_state
    return tuple(result_hashes)


def test_complete_matched_shadow_sequence_replays_byte_deterministically() -> None:
    first = _run_complete_sequence()
    second = _run_complete_sequence()
    assert first == second
    assert canonical_hash(first) == canonical_hash(second)
