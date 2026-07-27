from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading.domain.q1 import MatchedComparison, Q1ArmId
from trading.evaluation.matched import (
    DailyEvaluationObservation,
    EvaluationConfig,
    EvaluationError,
    evaluate_matched_attribution,
    evaluate_performance,
)
from trading.settlement.service import (
    BusinessCalendar,
    SettlementPolicy,
    SettlementProvenance,
    apply_settlement_events,
    record_buy_cash_debit,
    record_opening_settled_cash,
    record_sell_receivable,
    settle_due_receivables,
)

INSTANT = datetime(2026, 7, 24, 14, 0, tzinfo=UTC)


def _calendar() -> BusinessCalendar:
    return BusinessCalendar(
        version="nyse-calendar-v1",
        sessions=(
            date(2026, 7, 24),
            date(2026, 7, 27),
            date(2026, 7, 28),
            date(2026, 7, 29),
        ),
    )


def _settlement_policy() -> SettlementPolicy:
    return SettlementPolicy(
        version="settlement-tplus1-v1",
        calendar_version="nyse-calendar-v1",
        lag_business_sessions=1,
    )


def _settlement_provenance() -> SettlementProvenance:
    return SettlementProvenance(
        run_id="q1-test",
        source_cycle_id="cycle-1",
        config_manifest_hash="c" * 64,
        code_version="q1_math_core_v1",
        model_version="deterministic",
        source_manifest_hash="s" * 64,
        worker_fence_token="worker-1",
        cycle_attempt_count=1,
    )


def _evaluation_config(*, promotion_sessions: int = 126) -> EvaluationConfig:
    return EvaluationConfig(
        version="matched-v1",
        decimal_precision=40,
        result_quantum=Decimal("0.00000000000001"),
        annualization_sessions=252,
        risk_free_daily_return=Decimal("0"),
        downside_target_daily_return=Decimal("0"),
        newey_west_lag=2,
        bootstrap_samples=200,
        stationary_block_mean_length=Decimal("3"),
        bootstrap_confidence=Decimal("0.95"),
        bootstrap_seed=7077,
        promotion_min_common_sessions=promotion_sessions,
    )


def test_settlement_economic_identity_survives_cycle_lease_reclamation() -> None:
    first_provenance = _settlement_provenance()
    reclaimed_provenance = replace(
        first_provenance,
        worker_fence_token="worker-2",
        cycle_attempt_count=2,
    )
    first = record_opening_settled_cash(
        arm_id=Q1ArmId.Q1_DET,
        amount_usd=Decimal("1000"),
        effective_at=INSTANT,
        created_at=INSTANT,
        calendar_session_id="session-friday",
        policy=_settlement_policy(),
        provenance=first_provenance,
    )
    reclaimed = record_opening_settled_cash(
        arm_id=Q1ArmId.Q1_DET,
        amount_usd=Decimal("1000"),
        effective_at=INSTANT,
        created_at=INSTANT + timedelta(seconds=5),
        calendar_session_id="session-friday",
        policy=_settlement_policy(),
        provenance=reclaimed_provenance,
    )

    assert reclaimed.cash_settlement_event_id == first.cash_settlement_event_id
    assert reclaimed.idempotency_key == first.idempotency_key
    assert reclaimed.event_hash == first.event_hash
    assert reclaimed.cycle_attempt_count == 2


def _observation(
    arm_id: Q1ArmId,
    session_date: date,
    daily_return: str,
    *,
    cash_weight: str = "0.25",
    risk_active: bool = False,
    llm_active: bool = False,
) -> DailyEvaluationObservation:
    return DailyEvaluationObservation(
        session_date=session_date,
        arm_id=arm_id,
        net_daily_return=Decimal(daily_return),
        daily_turnover=Decimal("0.03"),
        commissions_usd=Decimal("0.10"),
        spread_cost_usd=Decimal("0.20"),
        delay_cost_usd=Decimal("0.05"),
        sensitivity_5bp_usd=Decimal("0.50"),
        sensitivity_10bp_usd=Decimal("1.00"),
        cash_weight=Decimal(cash_weight),
        qqq_weight=Decimal("0.50"),
        soxx_weight=Decimal("0.25"),
        risk_episode_active=risk_active,
        llm_reduction_active=llm_active,
    )


def test_settlement_uses_versioned_business_calendar_and_is_idempotent() -> None:
    policy = _settlement_policy()
    provenance = _settlement_provenance()
    opening = record_opening_settled_cash(
        arm_id=Q1ArmId.Q1_DET,
        amount_usd=Decimal("1000"),
        effective_at=INSTANT,
        created_at=INSTANT,
        calendar_session_id="session-friday",
        policy=policy,
        provenance=provenance,
    )
    sell = record_sell_receivable(
        arm_id=Q1ArmId.Q1_DET,
        fill_id="fill-sell",
        trade_at=INSTANT + timedelta(minutes=1),
        trade_session=date(2026, 7, 24),
        fill_notional_usd=Decimal("100"),
        commission_usd=Decimal("0.10"),
        created_at=INSTANT + timedelta(minutes=1),
        calendar_session_id="session-friday",
        policy=policy,
        calendar=_calendar(),
        provenance=provenance,
    )

    friday = apply_settlement_events(
        events=(opening, sell),
        as_of=INSTANT + timedelta(hours=1),
    )
    assert friday.settled_cash_usd == Decimal("1000")
    assert friday.unsettled_receivables_usd == Decimal("99.90")
    assert sell.settlement_date == date(2026, 7, 27)

    monday_at = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
    settlements = settle_due_receivables(
        events=(opening, sell),
        through_session=date(2026, 7, 27),
        effective_at=monday_at,
        created_at=monday_at,
        calendar_session_id="session-monday",
        policy=policy,
        calendar=_calendar(),
        provenance=provenance,
    )
    monday = apply_settlement_events(
        events=(opening, sell, *settlements),
        as_of=monday_at,
    )
    replay = settle_due_receivables(
        events=(opening, sell, *settlements),
        through_session=date(2026, 7, 27),
        effective_at=monday_at,
        created_at=monday_at,
        calendar_session_id="session-monday",
        policy=policy,
        calendar=_calendar(),
        provenance=provenance,
    )

    assert monday.settled_cash_usd == Decimal("1099.90")
    assert monday.unsettled_receivables_usd == 0
    assert replay == ()


def test_buy_consumes_settled_cash_only() -> None:
    policy = _settlement_policy()
    provenance = _settlement_provenance()
    opening = record_opening_settled_cash(
        arm_id=Q1ArmId.Q1_DET,
        amount_usd=Decimal("100"),
        effective_at=INSTANT,
        created_at=INSTANT,
        calendar_session_id="session-friday",
        policy=policy,
        provenance=provenance,
    )
    sell = record_sell_receivable(
        arm_id=Q1ArmId.Q1_DET,
        fill_id="fill-sell",
        trade_at=INSTANT + timedelta(minutes=1),
        trade_session=date(2026, 7, 24),
        fill_notional_usd=Decimal("100"),
        commission_usd=Decimal("0"),
        created_at=INSTANT + timedelta(minutes=1),
        calendar_session_id="session-friday",
        policy=policy,
        calendar=_calendar(),
        provenance=provenance,
    )
    buy = record_buy_cash_debit(
        arm_id=Q1ArmId.Q1_DET,
        fill_id="fill-buy",
        trade_at=INSTANT + timedelta(minutes=2),
        fill_notional_usd=Decimal("150"),
        commission_usd=Decimal("0"),
        created_at=INSTANT + timedelta(minutes=2),
        calendar_session_id="session-friday",
        policy=policy,
        provenance=provenance,
    )

    with pytest.raises(ValueError, match="settled cash"):
        apply_settlement_events(
            events=(opening, sell, buy),
            as_of=INSTANT + timedelta(hours=1),
        )


def test_late_created_backdated_cash_event_does_not_change_past_balance() -> None:
    policy = _settlement_policy()
    provenance = _settlement_provenance()
    opening = record_opening_settled_cash(
        arm_id=Q1ArmId.Q1_DET,
        amount_usd=Decimal("100"),
        effective_at=INSTANT,
        created_at=INSTANT,
        calendar_session_id="session-friday",
        policy=policy,
        provenance=provenance,
    )
    late = record_buy_cash_debit(
        arm_id=Q1ArmId.Q1_DET,
        fill_id="late-fill",
        trade_at=INSTANT + timedelta(minutes=1),
        fill_notional_usd=Decimal("10"),
        commission_usd=Decimal("0"),
        created_at=INSTANT + timedelta(hours=2),
        calendar_session_id="session-friday",
        policy=policy,
        provenance=provenance,
    )

    past = apply_settlement_events(
        events=(opening, late),
        as_of=INSTANT + timedelta(hours=1),
    )
    assert past.settled_cash_usd == Decimal("100")


def test_performance_reports_all_required_cost_and_exposure_metrics() -> None:
    observations = (
        _observation(
            Q1ArmId.Q1_DET,
            date(2026, 7, 27),
            "0.01",
            risk_active=True,
        ),
        _observation(
            Q1ArmId.Q1_DET,
            date(2026, 7, 28),
            "-0.005",
            risk_active=True,
        ),
        _observation(Q1ArmId.Q1_DET, date(2026, 7, 29), "0.002"),
    )
    result = evaluate_performance(observations, _evaluation_config())

    assert result.valid_sessions == 3
    assert result.cumulative_turnover == Decimal("0.09")
    assert result.commissions_usd == Decimal("0.30")
    assert result.spread_and_delay_cost_usd == Decimal("0.75")
    assert result.sensitivity_5bp_usd == Decimal("1.50")
    assert result.sensitivity_10bp_usd == Decimal("3.00")
    assert result.percentage_time_in_cash == Decimal("0.25")
    assert result.risk_episode_count == 1
    assert result.risk_episode_duration_sessions == 2
    assert result.maximum_drawdown > 0


def test_performance_hash_covers_cost_and_exposure_inputs() -> None:
    observation = _observation(
        Q1ArmId.Q1_DET,
        date(2026, 7, 27),
        "0.01",
    )
    changed_cost = replace(
        observation,
        commissions_usd=observation.commissions_usd + Decimal("0.01"),
    )

    first = evaluate_performance((observation,), _evaluation_config())
    second = evaluate_performance((changed_cost,), _evaluation_config())

    assert first.cumulative_return == second.cumulative_return
    assert first.result_hash != second.result_hash


def test_matched_attribution_is_deterministic_and_pair_is_exact() -> None:
    sessions = tuple(date(2026, 7, 1) + timedelta(days=index) for index in range(5))
    det = tuple(
        _observation(Q1ArmId.Q1_DET, session, value)
        for session, value in zip(
            sessions,
            ("0.01", "-0.01", "0.02", "0.00", "0.01"),
            strict=True,
        )
    )
    baseline = tuple(
        _observation(Q1ArmId.B0_VOL, session, value)
        for session, value in zip(
            sessions,
            ("0.005", "-0.008", "0.01", "0.001", "0.009"),
            strict=True,
        )
    )

    first = evaluate_matched_attribution(
        comparison=MatchedComparison.Q1_DET_MINUS_B0_VOL,
        left_observations=det,
        right_observations=baseline,
        config=_evaluation_config(),
    )
    second = evaluate_matched_attribution(
        comparison=MatchedComparison.Q1_DET_MINUS_B0_VOL,
        left_observations=reversed(det),
        right_observations=reversed(baseline),
        config=_evaluation_config(),
    )

    assert first.result_hash == second.result_hash
    assert first.bootstrap_lower == second.bootstrap_lower
    assert first.bootstrap_upper == second.bootstrap_upper
    assert first.left_arm_id is Q1ArmId.Q1_DET
    assert first.right_arm_id is Q1ArmId.B0_VOL
    assert first.promotion_is_manual is True
    assert first.claims_statistical_significance is False
    assert first.claims_profitability is False


def test_llm_attribution_is_only_q1_llm_minus_q1_det() -> None:
    session = date(2026, 7, 27)
    llm = (_observation(Q1ArmId.Q1_LLM, session, "0.001", llm_active=True),)
    det = (_observation(Q1ArmId.Q1_DET, session, "0.002"),)
    result = evaluate_matched_attribution(
        comparison=MatchedComparison.Q1_LLM_MINUS_Q1_DET,
        left_observations=llm,
        right_observations=det,
        config=_evaluation_config(),
    )

    assert result.daily_differences == (Decimal("-0.001"),)
    assert result.left_arm_id is Q1ArmId.Q1_LLM
    assert result.right_arm_id is Q1ArmId.Q1_DET

    with pytest.raises(EvaluationError, match="Expected"):
        evaluate_matched_attribution(
            comparison=MatchedComparison.Q1_LLM_MINUS_Q1_DET,
            left_observations=(
                _observation(Q1ArmId.B0_VOL, session, "0.001"),
            ),
            right_observations=det,
            config=_evaluation_config(),
        )


def test_promotion_readiness_requires_126_common_sessions_and_remains_manual() -> None:
    start = date(2026, 1, 1)
    sessions = tuple(start + timedelta(days=index) for index in range(126))
    det = tuple(_observation(Q1ArmId.Q1_DET, session, "0.001") for session in sessions)
    baseline = tuple(
        _observation(Q1ArmId.B0_VOL, session, "0.0005")
        for session in sessions
    )
    result = evaluate_matched_attribution(
        comparison=MatchedComparison.Q1_DET_MINUS_B0_VOL,
        left_observations=det,
        right_observations=baseline,
        config=_evaluation_config(),
    )

    assert result.common_valid_sessions == 126
    assert result.eligible_for_manual_promotion_review is True
    assert result.promotion_is_manual is True


def test_config_cannot_lower_promotion_gate_below_126() -> None:
    with pytest.raises(EvaluationError, match="126"):
        _evaluation_config(promotion_sessions=125)
