from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from trading.domain.hashing import canonical_hash
from trading.domain.q1 import (
    CashSettlementEvent,
    CashSettlementEventType,
    MarketCalendarSession,
    MatchedAttributionResult,
    MatchedComparison,
    OrderEvent,
    OrderEventType,
    PointInTimeSourceReference,
    Q1ArmId,
    Q1DecisionInputManifest,
    Q1StrategyDecision,
    RiskEpisode,
    RiskEpisodeEvent,
    RiskEpisodeEventType,
    RiskSeverity,
    RiskTarget,
    StrategyDailyResult,
    StrategyEvaluationAnchor,
)
from trading.persistence.models import (
    CashSettlementEventRow,
    OrderEventRow,
    OrderIntentRow,
    PaperCycleRow,
    RiskEpisodeEventRow,
    RiskEpisodeTargetRow,
    RunRow,
)
from trading.persistence.q1 import (
    CashSettlementRepository,
    MarketCalendarSessionRepository,
    OrderEventRepository,
    Q1CycleFenceError,
    Q1PersistenceConflict,
    Q1StrategyDecisionRepository,
    RiskEpisodeRepository,
    StrategyEvaluationAnchorRepository,
    StrategyEvaluationResultRepository,
)

HASH = "a" * 64
CODE_VERSION = "test-code"
MODEL_VERSION = "deterministic-none"
NOW = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)


def _version_fields() -> dict[str, str]:
    return {
        "config_manifest_hash": HASH,
        "code_version": CODE_VERSION,
        "model_version": MODEL_VERSION,
        "source_manifest_hash": HASH,
    }


def _calendar(
    *,
    available_at: datetime = NOW,
    close_at: datetime | None = None,
) -> MarketCalendarSession:
    resolved_close = close_at or NOW + timedelta(hours=6)
    payload = {
        "calendar_version": "XNYS-test-v1",
        "session_date": date(2026, 7, 27),
        "open_at": NOW - timedelta(minutes=30),
        "close_at": resolved_close,
        "available_at": available_at,
    }
    return MarketCalendarSession(
        calendar_session_id=f"calendar-{canonical_hash(payload)[:16]}",
        calendar_version="XNYS-test-v1",
        session_date=date(2026, 7, 27),
        open_at=NOW - timedelta(minutes=30),
        close_at=resolved_close,
        source="test-calendar",
        available_at=available_at,
        session_hash=canonical_hash(payload),
        created_at=available_at,
        **_version_fields(),
    )


def _seed_run_cycle_and_order(session) -> None:
    session.add(
        RunRow(
            run_id="q1-test-run",
            mode="PAPER",
            experiment_version="q1_math_core_v1",
            config_manifest_hash=HASH,
            code_commit=CODE_VERSION,
            started_at=NOW,
            ended_at=None,
            status="RUNNING",
            result_manifest=None,
            result_hash=None,
        )
    )
    session.flush()
    session.add(
        PaperCycleRow(
            cycle_id="q1-cycle-1",
            run_id="q1-test-run",
            cycle_kind="Q1_STRATEGIC",
            scheduled_at=NOW,
            data_available_cutoff=NOW,
            status="RUNNING",
            idempotency_key="q1-cycle-1",
            lease_owner="worker-1",
            lease_expires_at=NOW + timedelta(hours=1),
            attempt_count=1,
            input_manifest_hash=HASH,
            output_manifest_hash=None,
            started_at=NOW,
            completed_at=None,
            last_error_code=None,
            last_error_detail=None,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()
    session.add(
        OrderIntentRow(
            order_intent_id="q1-order-1",
            run_id="q1-test-run",
            arm_id=Q1ArmId.Q1_DET.value,
            source_cycle_id="q1-cycle-1",
            input_state_sequence=1,
            symbol="QQQ",
            side="BUY",
            quantity=Decimal("10"),
            created_at=NOW,
            valid_until=NOW + timedelta(minutes=20),
            decision_quote_id="quote-1",
            decision_reference_price=Decimal("500"),
            algorithm_version="q1_math_core_v1",
            config_manifest_hash=HASH,
            code_version=CODE_VERSION,
            model_version=MODEL_VERSION,
            source_manifest_hash=HASH,
            decision_spread_bps=Decimal("2"),
            idempotency_key="q1-order-1",
            payload_json={},
            intent_hash=HASH,
        )
    )
    session.flush()


def _order_event(
    *,
    sequence: int,
    event_type: OrderEventType,
    remaining: str,
    quantity_delta: str = "0",
    cumulative_fill: str = "0",
    commission_delta: str = "0",
    cumulative_commission: str = "0",
    event_id: str | None = None,
) -> OrderEvent:
    identity = event_id or f"q1-order-event-{sequence}"
    return OrderEvent(
        event_id=identity,
        order_intent_id="q1-order-1",
        event_type=event_type,
        event_sequence=sequence,
        quantity_delta=Decimal(quantity_delta),
        commission_delta_usd=Decimal(commission_delta),
        remaining_quantity=Decimal(remaining),
        cumulative_filled_quantity=Decimal(cumulative_fill),
        cumulative_commission_usd=Decimal(cumulative_commission),
        occurred_at=NOW + timedelta(seconds=sequence),
        available_at=NOW + timedelta(seconds=sequence),
        idempotency_key=identity,
        source_cycle_id="q1-cycle-1",
        worker_fence_token="worker-1",
        cycle_attempt_count=1,
        event_hash=canonical_hash({"event_id": identity}),
        **_version_fields(),
    )


def _risk_target(*, generation: int = 1, quantity: str = "5") -> RiskTarget:
    return RiskTarget(
        symbol="QQQ",
        target_quantity=Decimal(quantity),
        trigger_quote_id=f"quote-trigger-{generation}",
        target_generation=generation,
        trigger_quantity=Decimal("10"),
        trigger_price=Decimal("500"),
        target_weight=Decimal("0.5") if generation == 1 else Decimal("0"),
    )


def _risk_episode(calendar_session_id: str) -> RiskEpisode:
    target = _risk_target()
    return RiskEpisode(
        risk_episode_id="risk-episode-1",
        run_id="q1-test-run",
        arm_id=Q1ArmId.Q1_DET,
        severity=RiskSeverity.HARD_REDUCE,
        calendar_session_id=calendar_session_id,
        triggered_at=NOW + timedelta(seconds=1),
        trigger_nav_usd=Decimal("10000"),
        session_open_nav_usd=Decimal("11000"),
        running_peak_nav_usd=Decimal("12000"),
        daily_loss=Decimal("0.10"),
        run_drawdown=Decimal("0.1666666667"),
        portfolio_annualized_vol=Decimal("0.20"),
        soft_daily_threshold=Decimal("0.025"),
        hard_daily_threshold=Decimal("0.04"),
        reconciliation_status="OK",
        targets=(target,),
        target_manifest_hash=canonical_hash((target,)),
        episode_hash=canonical_hash({"episode": 1}),
        created_at=NOW + timedelta(seconds=1),
        **_version_fields(),
    )


def _risk_event(
    *,
    event_type: RiskEpisodeEventType,
    sequence: int,
    severity: RiskSeverity,
    targets: tuple[RiskTarget, ...] = (),
    generation: int = 1,
) -> RiskEpisodeEvent:
    event_id = f"risk-event-{sequence}"
    return RiskEpisodeEvent(
        risk_episode_event_id=event_id,
        risk_episode_id="risk-episode-1",
        event_type=event_type,
        event_sequence=sequence,
        severity=severity,
        target_generation=generation,
        occurred_at=NOW + timedelta(seconds=sequence + 1),
        available_at=NOW + timedelta(seconds=sequence + 1),
        targets=targets,
        source_cycle_id="q1-cycle-1",
        worker_fence_token="worker-1",
        cycle_attempt_count=1,
        idempotency_key=event_id,
        event_hash=canonical_hash({"event_id": event_id}),
        **_version_fields(),
    )


def _opening_cash_event(
    *,
    event_id: str = "cash-opening",
    amount: str = "10000",
    created_at: datetime = NOW,
    effective_at: datetime = NOW,
    attempt_count: int = 1,
) -> CashSettlementEvent:
    return CashSettlementEvent(
        cash_settlement_event_id=event_id,
        run_id="q1-test-run",
        arm_id=Q1ArmId.Q1_DET,
        event_type=CashSettlementEventType.OPENING_SETTLED_CASH,
        receivable_id=None,
        source_fill_id=None,
        settlement_policy_version="T1-business-calendar-v1",
        settled_cash_delta_usd=Decimal(amount),
        unsettled_receivable_delta_usd=Decimal("0"),
        gross_amount_usd=Decimal(amount),
        commission_usd=Decimal("0"),
        trade_at=None,
        settlement_date=None,
        effective_at=effective_at,
        calendar_session_id="calendar-placeholder",
        source_cycle_id="q1-cycle-1",
        worker_fence_token="worker-1",
        cycle_attempt_count=attempt_count,
        idempotency_key=event_id,
        event_hash=canonical_hash({"cash": event_id}),
        created_at=created_at,
        **_version_fields(),
    )


def _seed_all_q1_immutable_rows(session) -> dict[str, tuple[str, str]]:
    _seed_run_cycle_and_order(session)
    calendar = MarketCalendarSessionRepository(session).append(_calendar())
    anchor = StrategyEvaluationAnchor(
        evaluation_anchor_id="anchor-all",
        run_id="q1-test-run",
        calendar_session_id=calendar.calendar_session_id,
        common_t0_at=NOW,
        initial_nav_usd=Decimal("10000"),
        quote_manifest_hash=HASH,
        anchor_hash=canonical_hash({"anchor": "all"}),
        created_at=NOW,
        **_version_fields(),
    )
    StrategyEvaluationAnchorRepository(session).append(anchor)
    OrderEventRepository(session).append(
        _order_event(
            sequence=1,
            event_type=OrderEventType.CREATED,
            remaining="10",
        )
    )
    episode = _risk_episode(calendar.calendar_session_id)
    RiskEpisodeRepository(session).append_episode(
        episode,
        _risk_event(
            event_type=RiskEpisodeEventType.ACTIVATE,
            sequence=1,
            severity=RiskSeverity.HARD_REDUCE,
            targets=episode.targets,
        ),
    )
    opening = _opening_cash_event().model_copy(
        update={"calendar_session_id": calendar.calendar_session_id}
    )
    CashSettlementRepository(session).append(opening)
    evaluation = StrategyEvaluationResultRepository(session)
    evaluation.append_daily(
        StrategyDailyResult(
            strategy_daily_result_id="daily-result-1",
            evaluation_anchor_id=anchor.evaluation_anchor_id,
            run_id="q1-test-run",
            arm_id=Q1ArmId.Q1_DET,
            calendar_session_id=calendar.calendar_session_id,
            session_date=calendar.session_date,
            valuation_at=NOW,
            nav_usd=Decimal("10000"),
            net_daily_return=Decimal("0"),
            cumulative_return=Decimal("0"),
            daily_turnover=Decimal("0"),
            cumulative_turnover=Decimal("0"),
            commissions_usd=Decimal("0"),
            spread_cost_usd=Decimal("0"),
            delay_cost_usd=Decimal("0"),
            sensitivity_5bp_usd=Decimal("0"),
            sensitivity_10bp_usd=Decimal("0"),
            cash_weight=Decimal("1"),
            qqq_weight=Decimal("0"),
            soxx_weight=Decimal("0"),
            active_risk_episode_count=1,
            active_llm_reduction_count=0,
            result_hash=canonical_hash({"daily": 1}),
            created_at=NOW,
            **_version_fields(),
        )
    )
    evaluation.append_matched(
        MatchedAttributionResult(
            matched_attribution_result_id="matched-result-1",
            evaluation_anchor_id=anchor.evaluation_anchor_id,
            run_id="q1-test-run",
            comparison=MatchedComparison.Q1_DET_MINUS_B0_VOL,
            left_arm_id=Q1ArmId.Q1_DET,
            right_arm_id=Q1ArmId.B0_VOL,
            through_session_date=calendar.session_date,
            common_valid_sessions=0,
            mean_daily_difference=Decimal("0"),
            annualized_difference=Decimal("0"),
            newey_west_lag=5,
            newey_west_standard_error=Decimal("0"),
            bootstrap_seed=7077,
            bootstrap_lower=Decimal("0"),
            bootstrap_upper=Decimal("0"),
            promotion_ready=False,
            result_hash=canonical_hash({"matched": 1}),
            created_at=NOW,
            **_version_fields(),
        )
    )
    session.flush()
    target_id = session.scalar(select(RiskEpisodeTargetRow.risk_target_id))
    assert target_id is not None
    return {
        "market_calendar_sessions": (
            "calendar_session_id",
            calendar.calendar_session_id,
        ),
        "strategy_evaluation_anchors": ("evaluation_anchor_id", "anchor-all"),
        "risk_episodes": ("risk_episode_id", "risk-episode-1"),
        "risk_episode_targets": ("risk_target_id", target_id),
        "risk_episode_events": ("risk_episode_event_id", "risk-event-1"),
        "order_events": ("order_event_id", "q1-order-event-1"),
        "cash_settlement_events": (
            "cash_settlement_event_id",
            "cash-opening",
        ),
        "strategy_daily_results": (
            "strategy_daily_result_id",
            "daily-result-1",
        ),
        "matched_attribution_results": (
            "matched_attribution_result_id",
            "matched-result-1",
        ),
    }


def test_calendar_and_anchor_are_point_in_time_and_immutable(sqlite_database) -> None:
    _, engine, factory = sqlite_database
    with factory.begin() as session:
        _seed_run_cycle_and_order(session)
        calendar = _calendar()
        calendar_row = MarketCalendarSessionRepository(session).append(calendar)
        anchor = StrategyEvaluationAnchor(
            evaluation_anchor_id="anchor-1",
            run_id="q1-test-run",
            calendar_session_id=calendar_row.calendar_session_id,
            common_t0_at=NOW,
            initial_nav_usd=Decimal("10000"),
            quote_manifest_hash=HASH,
            anchor_hash=canonical_hash({"anchor": 1}),
            created_at=NOW,
            **_version_fields(),
        )
        StrategyEvaluationAnchorRepository(session).append(anchor)

        assert (
            MarketCalendarSessionRepository(session).for_date_as_of(
                calendar_version=calendar.calendar_version,
                session_date=calendar.session_date,
                cutoff=NOW - timedelta(microseconds=1),
            )
            is None
        )
        assert (
            MarketCalendarSessionRepository(session).for_date_as_of(
                calendar_version=calendar.calendar_version,
                session_date=calendar.session_date,
                cutoff=NOW,
            )
            is calendar_row
        )
        revision = _calendar(
            available_at=NOW + timedelta(hours=1),
            close_at=NOW + timedelta(hours=3),
        )
        revision_row = MarketCalendarSessionRepository(session).append(
            revision
        )
        assert revision_row.calendar_session_id != (
            calendar_row.calendar_session_id
        )
        assert (
            MarketCalendarSessionRepository(session).for_date_as_of(
                calendar_version=calendar.calendar_version,
                session_date=calendar.session_date,
                cutoff=NOW + timedelta(minutes=30),
            )
            is calendar_row
        )
        assert (
            MarketCalendarSessionRepository(session).for_date_as_of(
                calendar_version=calendar.calendar_version,
                session_date=calendar.session_date,
                cutoff=NOW + timedelta(hours=1),
            )
            is revision_row
        )

    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text(
                "UPDATE strategy_evaluation_anchors "
                "SET initial_nav_usd=1 WHERE evaluation_anchor_id='anchor-1'"
            )
        )


def test_calendar_reobservation_is_idempotent_by_stable_source_id(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    original = _calendar()
    reobserved = original.model_copy(
        update={
            "available_at": NOW + timedelta(hours=1),
            "created_at": NOW + timedelta(hours=1),
            "config_manifest_hash": "b" * 64,
            "code_version": "later-code",
            "source_manifest_hash": "c" * 64,
            "session_hash": "d" * 64,
        }
    )
    conflicting_hours = reobserved.model_copy(
        update={"close_at": original.close_at - timedelta(hours=1)}
    )

    with factory.begin() as session:
        repository = MarketCalendarSessionRepository(session)
        first = repository.append(original)
        repeated = repository.append(reobserved)

        assert repeated is first
        assert repeated.available_at == original.available_at
        with pytest.raises(
            Q1PersistenceConflict,
            match="different immutable market hours",
        ):
            repository.append(conflicting_hours)


def test_pending_orders_come_only_from_latest_order_event(sqlite_database) -> None:
    _, _, factory = sqlite_database
    with factory.begin() as session:
        _seed_run_cycle_and_order(session)
        repository = OrderEventRepository(session)
        created = _order_event(
            sequence=1,
            event_type=OrderEventType.CREATED,
            remaining="10",
        )
        repository.append(created)
        session.flush()
        pending = repository.pending(run_id="q1-test-run")
        assert [item.order.order_intent_id for item in pending] == ["q1-order-1"]

        repository.append(
            _order_event(
                sequence=2,
                event_type=OrderEventType.BLOCKED_BY_PRICE_GUARD,
                remaining="10",
            )
        )
        session.flush()
        assert len(repository.pending(run_id="q1-test-run")) == 1

        repository.append(
            _order_event(
                sequence=3,
                event_type=OrderEventType.PARTIALLY_FILLED,
                remaining="6",
                quantity_delta="4",
                cumulative_fill="4",
                commission_delta="1",
                cumulative_commission="1",
            )
        )
        session.flush()
        assert repository.pending(run_id="q1-test-run")[0].latest_event.remaining_quantity == 6

        repository.append(
            _order_event(
                sequence=4,
                event_type=OrderEventType.SUPERSEDED,
                remaining="6",
                cumulative_fill="4",
                cumulative_commission="1",
            )
        )
        session.flush()
        assert repository.pending(run_id="q1-test-run") == []
        assert (
            repository.append(
                _order_event(
                    sequence=4,
                    event_type=OrderEventType.SUPERSEDED,
                    remaining="6",
                    cumulative_fill="4",
                    cumulative_commission="1",
                )
            ).order_event_id
            == "q1-order-event-4"
        )
        assert session.scalar(select(func.count()).select_from(OrderEventRow)) == 4


def test_strategy_decision_persists_distinct_pit_times_and_manifest(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    with factory.begin() as session:
        _seed_run_cycle_and_order(session)
        calendar = MarketCalendarSessionRepository(session).append(_calendar())
        manifest = Q1DecisionInputManifest(
            calendar_session_id=calendar.calendar_session_id,
            source_bars=(
                PointInTimeSourceReference(
                    record_id="bar-qqq-completed",
                    available_at=NOW - timedelta(days=1),
                ),
            ),
            quotes=(
                PointInTimeSourceReference(
                    record_id="quote-current",
                    available_at=NOW,
                ),
            ),
            manifest_hash=canonical_hash({"manifest": 1}),
            **_version_fields(),
        )
        decision = Q1StrategyDecision(
            portfolio_decision_id="q1-decision-1",
            run_id="q1-test-run",
            arm_id=Q1ArmId.Q1_DET,
            source_cycle_id="q1-cycle-1",
            input_state_sequence=1,
            decision_kind="STRATEGIC_TARGET",
            scheduled_at=NOW,
            signal_data_cutoff=NOW,
            portfolio_state_as_of=NOW,
            quote_as_of=NOW,
            decision_created_at=NOW + timedelta(seconds=1),
            valid_until=NOW + timedelta(minutes=20),
            input_manifest=manifest,
            target_weights={
                "QQQ": Decimal("0.5"),
                "SOXX": Decimal("0"),
                "USD_CASH": Decimal("0.5"),
            },
            diagnostics={},
            worker_fence_token="worker-1",
            cycle_attempt_count=1,
            decision_hash=canonical_hash({"decision": 1}),
            **_version_fields(),
        )
        row = Q1StrategyDecisionRepository(session).append(decision)
        session.flush()

        assert row.scheduled_at == NOW
        assert row.signal_data_cutoff == NOW
        assert row.portfolio_state_as_of == NOW
        assert row.quote_as_of == NOW
        assert row.decision_created_at == NOW + timedelta(seconds=1)
        assert row.valid_until == NOW + timedelta(minutes=20)
        assert row.input_manifest_hash == manifest.manifest_hash
        assert row.calendar_session_id == calendar.calendar_session_id


def test_strategy_decision_rejects_future_source_records() -> None:
    manifest = Q1DecisionInputManifest(
        calendar_session_id="calendar-1",
        source_bars=(
            PointInTimeSourceReference(
                record_id="future-bar",
                available_at=NOW + timedelta(seconds=1),
            ),
        ),
        quotes=(),
        manifest_hash=HASH,
        **_version_fields(),
    )
    with pytest.raises(ValidationError, match="bar unavailable"):
        Q1StrategyDecision(
            portfolio_decision_id="q1-decision-future",
            run_id="q1-test-run",
            arm_id=Q1ArmId.Q1_DET,
            source_cycle_id="q1-cycle-1",
            input_state_sequence=1,
            decision_kind="STRATEGIC_TARGET",
            scheduled_at=NOW,
            signal_data_cutoff=NOW,
            portfolio_state_as_of=NOW,
            quote_as_of=NOW,
            decision_created_at=NOW + timedelta(seconds=1),
            valid_until=NOW + timedelta(minutes=20),
            input_manifest=manifest,
            target_weights={"USD_CASH": Decimal("1")},
            diagnostics={},
            worker_fence_token="worker-1",
            cycle_attempt_count=1,
            decision_hash=HASH,
            **_version_fields(),
        )


def test_stale_cycle_cannot_append_order_event(sqlite_database) -> None:
    _, _, factory = sqlite_database
    with factory.begin() as session:
        _seed_run_cycle_and_order(session)
        event = _order_event(
            sequence=1,
            event_type=OrderEventType.CREATED,
            remaining="10",
        ).model_copy(update={"cycle_attempt_count": 2})
        with pytest.raises(Q1CycleFenceError, match="attempt 2"):
            OrderEventRepository(session).append(event)


def test_risk_episode_has_typed_latched_targets_and_escalation(sqlite_database) -> None:
    _, _, factory = sqlite_database
    with factory.begin() as session:
        _seed_run_cycle_and_order(session)
        calendar = MarketCalendarSessionRepository(session).append(_calendar())
        episode = _risk_episode(calendar.calendar_session_id)
        activation = _risk_event(
            event_type=RiskEpisodeEventType.ACTIVATE,
            sequence=1,
            severity=RiskSeverity.HARD_REDUCE,
            targets=episode.targets,
        )
        repository = RiskEpisodeRepository(session)
        repository.append_episode(episode, activation)
        session.flush()

        active = repository.active(
            run_id="q1-test-run",
            arm_id=Q1ArmId.Q1_DET.value,
        )
        assert active is not None
        assert [(target.symbol, target.target_quantity) for target in active.targets] == [
            ("QQQ", Decimal("5")),
        ]

        critical_target = _risk_target(generation=2, quantity="0")
        repository.append_event(
            _risk_event(
                event_type=RiskEpisodeEventType.ESCALATE,
                sequence=2,
                severity=RiskSeverity.CRITICAL_EXIT,
                targets=(critical_target,),
                generation=2,
            )
        )
        session.flush()
        active = repository.active(
            run_id="q1-test-run",
            arm_id=Q1ArmId.Q1_DET.value,
        )
        assert active is not None
        assert active.latest_event.severity == RiskSeverity.CRITICAL_EXIT.value
        assert active.targets[0].target_quantity == 0
        assert session.scalar(select(func.count()).select_from(RiskEpisodeTargetRow)) == 2
        assert session.scalar(select(func.count()).select_from(RiskEpisodeEventRow)) == 2


def test_empty_risk_episode_and_empty_activation_are_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        _risk_episode("calendar-1").model_copy(update={"targets": ()})
        RiskEpisode.model_validate(
            _risk_episode("calendar-1").model_dump(mode="python") | {"targets": ()}
        )
    with pytest.raises(ValidationError, match="non-empty typed targets"):
        _risk_event(
            event_type=RiskEpisodeEventType.ACTIVATE,
            sequence=1,
            severity=RiskSeverity.HARD_REDUCE,
        )


def test_settlement_events_project_settled_and_unsettled_cash(sqlite_database) -> None:
    _, _, factory = sqlite_database
    with factory.begin() as session:
        _seed_run_cycle_and_order(session)
        calendar = MarketCalendarSessionRepository(session).append(_calendar())
        repository = CashSettlementRepository(session)
        opening = CashSettlementEvent(
            cash_settlement_event_id="cash-opening",
            run_id="q1-test-run",
            arm_id=Q1ArmId.Q1_DET,
            event_type=CashSettlementEventType.OPENING_SETTLED_CASH,
            receivable_id=None,
            source_fill_id=None,
            settlement_policy_version="T1-business-calendar-v1",
            settled_cash_delta_usd=Decimal("10000"),
            unsettled_receivable_delta_usd=Decimal("0"),
            gross_amount_usd=Decimal("10000"),
            commission_usd=Decimal("0"),
            trade_at=None,
            settlement_date=None,
            effective_at=NOW,
            calendar_session_id=calendar.calendar_session_id,
            source_cycle_id="q1-cycle-1",
            worker_fence_token="worker-1",
            cycle_attempt_count=1,
            idempotency_key="cash-opening",
            event_hash=canonical_hash({"cash": "opening"}),
            created_at=NOW,
            **_version_fields(),
        )
        sale = CashSettlementEvent(
            cash_settlement_event_id="cash-sale",
            run_id="q1-test-run",
            arm_id=Q1ArmId.Q1_DET,
            event_type=CashSettlementEventType.SELL_RECEIVABLE_CREATED,
            receivable_id="receivable-1",
            source_fill_id=None,
            settlement_policy_version="T1-business-calendar-v1",
            settled_cash_delta_usd=Decimal("0"),
            unsettled_receivable_delta_usd=Decimal("999"),
            gross_amount_usd=Decimal("1000"),
            commission_usd=Decimal("1"),
            trade_at=NOW,
            settlement_date=date(2026, 7, 28),
            effective_at=NOW + timedelta(minutes=1),
            calendar_session_id=calendar.calendar_session_id,
            source_cycle_id="q1-cycle-1",
            worker_fence_token="worker-1",
            cycle_attempt_count=1,
            idempotency_key="cash-sale",
            event_hash=canonical_hash({"cash": "sale"}),
            created_at=NOW + timedelta(minutes=1),
            **_version_fields(),
        )
        repository.append(opening)
        repository.append(sale)
        repository.append(sale)
        session.flush()

        balances = repository.balances(
            run_id="q1-test-run",
            arm_id=Q1ArmId.Q1_DET.value,
            as_of=NOW + timedelta(minutes=2),
        )
        assert balances.settled_cash_usd == 10000
        assert balances.unsettled_receivables_usd == 999
        assert balances.total_cash_usd == 10999
        assert (
            session.scalar(select(func.count()).select_from(CashSettlementEventRow))
            == 2
        )


def test_settlement_projection_excludes_late_created_backdated_event(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    with factory.begin() as session:
        _seed_run_cycle_and_order(session)
        calendar = MarketCalendarSessionRepository(session).append(_calendar())
        repository = CashSettlementRepository(session)
        opening = _opening_cash_event().model_copy(
            update={"calendar_session_id": calendar.calendar_session_id}
        )
        late_debit = CashSettlementEvent(
            cash_settlement_event_id="late-buy-debit",
            run_id="q1-test-run",
            arm_id=Q1ArmId.Q1_DET,
            event_type=CashSettlementEventType.BUY_SETTLED_CASH_DEBIT,
            receivable_id=None,
            source_fill_id=None,
            settlement_policy_version="T1-business-calendar-v1",
            settled_cash_delta_usd=Decimal("-10"),
            unsettled_receivable_delta_usd=Decimal("0"),
            gross_amount_usd=Decimal("10"),
            commission_usd=Decimal("0"),
            trade_at=NOW,
            settlement_date=None,
            effective_at=NOW,
            calendar_session_id=calendar.calendar_session_id,
            source_cycle_id="q1-cycle-1",
            worker_fence_token="worker-1",
            cycle_attempt_count=1,
            idempotency_key="late-buy-debit",
            event_hash=canonical_hash({"cash": "late-buy-debit"}),
            created_at=NOW + timedelta(minutes=30),
            **_version_fields(),
        )
        repository.append(opening)
        repository.append(late_debit)
        session.flush()

        past = repository.balances(
            run_id="q1-test-run",
            arm_id=Q1ArmId.Q1_DET.value,
            as_of=NOW + timedelta(minutes=10),
        )
        later = repository.balances(
            run_id="q1-test-run",
            arm_id=Q1ArmId.Q1_DET.value,
            as_of=NOW + timedelta(minutes=40),
        )
        assert past.settled_cash_usd == 10000
        assert later.settled_cash_usd == 9990


def test_stale_cycle_cannot_append_settlement_event(sqlite_database) -> None:
    _, _, factory = sqlite_database
    with factory.begin() as session:
        _seed_run_cycle_and_order(session)
        calendar = MarketCalendarSessionRepository(session).append(_calendar())
        stale = _opening_cash_event(
            event_id="stale-cash-opening",
            amount="1",
            attempt_count=2,
        ).model_copy(update={"calendar_session_id": calendar.calendar_session_id})
        with pytest.raises(Q1CycleFenceError, match="attempt 2"):
            CashSettlementRepository(session).append(stale)


def test_every_q1_immutable_row_rejects_update_and_delete(sqlite_database) -> None:
    _, engine, factory = sqlite_database
    with factory.begin() as session:
        identities = _seed_all_q1_immutable_rows(session)

    for table_name, (key_column, key_value) in identities.items():
        with (
            engine.connect() as connection,
            connection.begin(),
            pytest.raises(DBAPIError, match="append-only"),
        ):
            connection.execute(
                text(
                    f"UPDATE {table_name} SET {key_column}={key_column} "
                    f"WHERE {key_column}=:key_value"
                ),
                {"key_value": key_value},
            )
        with (
            engine.connect() as connection,
            connection.begin(),
            pytest.raises(DBAPIError, match="append-only"),
        ):
            connection.execute(
                text(
                    f"DELETE FROM {table_name} WHERE {key_column}=:key_value"
                ),
                {"key_value": key_value},
            )


def test_q1_unique_constraints_exist_in_real_sqlite_schema(sqlite_database) -> None:
    _, engine, factory = sqlite_database
    with factory.begin() as session:
        _seed_all_q1_immutable_rows(session)

    expected = {
        "market_calendar_sessions": {
            ("calendar_version", "session_date", "session_hash")
        },
        "strategy_evaluation_anchors": {
            ("run_id",),
            ("anchor_hash",),
        },
        "risk_episodes": {("run_id", "arm_id", "episode_hash")},
        "risk_episode_targets": {
            ("risk_episode_id", "symbol", "target_generation")
        },
        "risk_episode_events": {
            ("risk_episode_id", "event_sequence"),
            ("idempotency_key",),
        },
        "order_events": {
            ("order_intent_id", "event_sequence"),
            ("idempotency_key",),
        },
        "cash_settlement_events": {
            ("idempotency_key",),
            ("receivable_id", "event_type"),
            ("source_fill_id", "event_type"),
        },
        "strategy_daily_results": {("run_id", "arm_id", "session_date")},
        "matched_attribution_results": {
            ("run_id", "comparison", "through_session_date")
        },
    }
    with engine.connect() as connection:
        for table_name, expected_columns in expected.items():
            actual: set[tuple[str, ...]] = set()
            indexes = connection.execute(
                text(f"PRAGMA index_list('{table_name}')")
            ).mappings()
            for index in indexes:
                if not bool(index["unique"]):
                    continue
                index_name = str(index["name"]).replace("'", "''")
                columns = tuple(
                    str(row["name"])
                    for row in connection.execute(
                        text(f"PRAGMA index_info('{index_name}')")
                    ).mappings()
                )
                actual.add(columns)
            assert expected_columns <= actual, table_name


@pytest.mark.parametrize(
    ("table_name", "primary_key", "sequence_column"),
    [
        ("order_events", "order_event_id", "event_sequence"),
        (
            "risk_episode_events",
            "risk_episode_event_id",
            "event_sequence",
        ),
        ("cash_settlement_events", "cash_settlement_event_id", None),
    ],
)
def test_event_idempotency_keys_are_enforced_by_sqlite(
    sqlite_database,
    table_name: str,
    primary_key: str,
    sequence_column: str | None,
) -> None:
    _, engine, factory = sqlite_database
    with factory.begin() as session:
        _seed_all_q1_immutable_rows(session)

    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="idempotency_key"),
    ):
        row = dict(
            connection.execute(
                text(f"SELECT * FROM {table_name} LIMIT 1")
            ).mappings().one()
        )
        row[primary_key] = f"duplicate-{table_name}"
        if sequence_column is not None:
            row[sequence_column] = int(row[sequence_column]) + 100
        columns = tuple(row)
        connection.execute(
            text(
                f"INSERT INTO {table_name} "
                f"({', '.join(columns)}) VALUES "
                f"({', '.join(f':{column}' for column in columns)})"
            ),
            row,
        )


@pytest.mark.parametrize(
    "table_name",
    [
        "market_calendar_sessions",
        "strategy_evaluation_anchors",
        "risk_episodes",
        "risk_episode_targets",
        "risk_episode_events",
        "order_events",
        "cash_settlement_events",
        "strategy_daily_results",
        "matched_attribution_results",
    ],
)
def test_all_q1_entities_have_sqlite_append_only_guards(
    sqlite_database,
    table_name: str,
) -> None:
    _, engine, _ = sqlite_database
    with engine.connect() as connection:
        trigger_names = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND tbl_name=:table_name"
                ),
                {"table_name": table_name},
            )
        }
    assert trigger_names == {
        f"trg_{table_name}_no_update",
        f"trg_{table_name}_no_delete",
    }
