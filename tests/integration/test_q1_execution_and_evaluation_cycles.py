from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import FEED, PROVIDER
from trading.data.market_repository import MarketDataRepository
from trading.domain.contracts import MarketBar, MarketQuote
from trading.domain.enums import (
    MarketConnectionState,
    MarketDataSourceKind,
    OrderSide,
)
from trading.domain.hashing import canonical_hash
from trading.domain.q1 import (
    CashSettlementEventType,
    MatchedComparison,
    OrderEventType,
    Q1ArmId,
    StrategyEvaluationAnchor,
)
from trading.domain.q1_runtime import Q1OrderIntent
from trading.execution.order_state import (
    OrderDescriptor,
    OrderEventProvenance,
    Q1OrderClass,
    append_order_event,
    pending_orders,
)
from trading.persistence.models import (
    ArmStateSnapshotRow,
    CashSettlementEventRow,
    FillRow,
    MatchedAttributionResultRow,
    OrderEventRow,
    PaperCycleRow,
    RunRow,
    StrategyDailyResultRow,
)
from trading.persistence.q1 import (
    OrderEventRepository,
    StrategyEvaluationAnchorRepository,
)
from trading.persistence.q1_runtime import (
    Q1StaleWorkerError,
    append_arm_state,
    append_order_intent,
    latest_arm_state,
    load_q1_order_book,
)
from trading.runtime.q1_evaluation_cycle import Q1EvaluationCycleProcessor
from trading.runtime.q1_execution_cycle import Q1ExecutionCycleProcessor
from trading.runtime.q1_paper import Q1_MODEL_VERSION, Q1PaperRuntimeService
from trading.runtime.q1_scheduler import VersionedMarketSession
from trading.runtime.q1_state import Q1ArmState
from trading.settings import load_q1_config_bundle

RUN_ID = "q1-cycle-integration"
ANCHOR_ID = "q1-cycle-integration-anchor"
WORKER = "q1-cycle-integration-worker"
SESSION_DATE = date(2026, 7, 27)
SESSION_OPEN = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
SESSION_CLOSE = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
ORDER_CREATED_AT = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
EXECUTION_AT = datetime(2026, 7, 27, 14, 2, tzinfo=UTC)
HASH = "a" * 64


@dataclass(frozen=True, slots=True)
class _ExecutionCase:
    runtime: Q1PaperRuntimeService
    processor: Q1ExecutionCycleProcessor
    cycle: PaperCycleRow
    calendar: VersionedMarketSession
    order_intent_id: str


def _runtime(
    factory: sessionmaker[Session],
    *,
    repository_root: Path,
) -> Q1PaperRuntimeService:
    return Q1PaperRuntimeService(
        factory,
        config=load_q1_config_bundle(repository_root / "config"),
        workspace_root=repository_root,
    )


def _calendar(
    session_date: date,
    *,
    open_at: datetime,
    close_at: datetime,
) -> VersionedMarketSession:
    return VersionedMarketSession(
        calendar_session_id=f"q1-calendar-{session_date.isoformat()}",
        calendar_version="alpaca_market_calendar_v1",
        session_date=session_date,
        open_at=open_at,
        close_at=close_at,
        source_payload_hash=canonical_hash(
            {
                "session_date": session_date,
                "open_at": open_at,
                "close_at": close_at,
            }
        ),
        source_available_at=open_at - timedelta(days=7),
    )


def _session_calendar() -> VersionedMarketSession:
    return _calendar(
        SESSION_DATE,
        open_at=SESSION_OPEN,
        close_at=SESSION_CLOSE,
    )


def _seed_run_and_cycle(
    factory: sessionmaker[Session],
    *,
    cycle_id: str,
    cycle_kind: str,
    scheduled_at: datetime,
    lease_owner: str = WORKER,
    attempt_count: int = 1,
) -> PaperCycleRow:
    with factory.begin() as session:
        if session.get(RunRow, RUN_ID) is None:
            session.add(
                RunRow(
                    run_id=RUN_ID,
                    mode="PAPER",
                    experiment_version="q1_math_core_v1",
                    config_manifest_hash=HASH,
                    code_commit="test-code",
                    started_at=SESSION_OPEN,
                    ended_at=None,
                    status="RUNNING",
                    result_manifest={"real_order_routing": False},
                    result_hash=None,
                )
            )
            session.flush()
        cycle = PaperCycleRow(
            cycle_id=cycle_id,
            run_id=RUN_ID,
            cycle_kind=cycle_kind,
            scheduled_at=scheduled_at,
            data_available_cutoff=scheduled_at,
            status="RUNNING",
            idempotency_key=cycle_id,
            lease_owner=lease_owner,
            lease_expires_at=scheduled_at + timedelta(hours=1),
            attempt_count=attempt_count,
            input_manifest_hash=None,
            output_manifest_hash=None,
            started_at=scheduled_at,
            completed_at=None,
            last_error_code=None,
            last_error_detail=None,
            created_at=scheduled_at,
            updated_at=scheduled_at,
        )
        session.add(cycle)
    return cycle


def _register_calendars(
    runtime: Q1PaperRuntimeService,
    *,
    include_next_session: bool,
) -> VersionedMarketSession:
    current = _session_calendar()
    runtime.register_calendar_session(
        current,
        now=SESSION_OPEN - timedelta(days=1),
    )
    if include_next_session:
        next_session = _calendar(
            SESSION_DATE + timedelta(days=1),
            open_at=SESSION_OPEN + timedelta(days=1),
            close_at=SESSION_CLOSE + timedelta(days=1),
        )
        runtime.register_calendar_session(
            next_session,
            now=SESSION_OPEN - timedelta(days=1),
        )
    return current


def _intent(
    runtime: Q1PaperRuntimeService,
    *,
    cycle_id: str,
    valid_until: datetime,
    order_intent_id: str = "q1-cycle-order",
) -> Q1OrderIntent:
    values: dict[str, object] = {
        "order_intent_id": order_intent_id,
        "run_id": RUN_ID,
        "arm_id": Q1ArmId.Q1_DET,
        "portfolio_decision_id": "q1-cycle-decision",
        "risk_decision_id": "q1-cycle-risk-approval",
        "source_cycle_id": cycle_id,
        "input_state_sequence": 0,
        "symbol": "QQQ",
        "side": OrderSide.SELL,
        "order_class": Q1OrderClass.NORMAL.value,
        "quantity": Decimal("100"),
        "decision_quote_id": "q1-decision-quote",
        "decision_reference_price": Decimal("100"),
        "decision_spread_bps": Decimal("10"),
        "created_at": ORDER_CREATED_AT,
        "valid_until": valid_until,
        "idempotency_key": f"{order_intent_id}-idempotency",
        "algorithm_version": "q1_math_core_v1",
        "config_manifest_hash": runtime.config.manifest_hash,
        "code_version": "test-code",
        "model_version": Q1_MODEL_VERSION,
        "source_manifest_hash": HASH,
    }
    return Q1OrderIntent(
        **values,
        intent_hash=canonical_hash(values),
    )


def _descriptor(intent: Q1OrderIntent) -> OrderDescriptor:
    return OrderDescriptor(
        order_intent_id=intent.order_intent_id,
        arm_id=intent.arm_id.value,
        portfolio_decision_id=intent.portfolio_decision_id,
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        order_class=Q1OrderClass(intent.order_class),
        created_at=intent.created_at,
        valid_until=intent.valid_until,
    )


def _seed_order_and_state(
    factory: sessionmaker[Session],
    *,
    runtime: Q1PaperRuntimeService,
    cycle: PaperCycleRow,
    valid_until: datetime,
) -> Q1OrderIntent:
    intent = _intent(
        runtime,
        cycle_id=cycle.cycle_id,
        valid_until=valid_until,
    )
    provenance = OrderEventProvenance(
        config_manifest_hash=runtime.config.manifest_hash,
        code_version="test-code",
        model_version=Q1_MODEL_VERSION,
        source_manifest_hash=HASH,
        worker_fence_token=WORKER,
        cycle_attempt_count=1,
    )
    created = append_order_event(
        order=_descriptor(intent),
        existing_events=(),
        event_type=OrderEventType.CREATED,
        occurred_at=ORDER_CREATED_AT,
        available_at=ORDER_CREATED_AT,
        provenance=provenance,
        source_cycle_id=cycle.cycle_id,
    )
    opening_state = Q1ArmState(
        arm_id=Q1ArmId.Q1_DET.value,
        initial_nav_usd=Decimal("10000"),
        settled_cash_usd=Decimal("0"),
        unsettled_receivables=(),
        positions={"QQQ": Decimal("100")},
        sequence=0,
        evaluation_anchor_id=ANCHOR_ID,
    )
    with factory.begin() as session:
        append_order_intent(session, intent)
        OrderEventRepository(session).append(created)
        append_arm_state(
            session,
            run_id=RUN_ID,
            state=opening_state,
            source_cycle_id=cycle.cycle_id,
            created_at=ORDER_CREATED_AT,
            expected_previous_sequence=None,
        )
    return intent


def _market_bar(index: int) -> MarketBar:
    session_date = SESSION_DATE - timedelta(days=30 - index)
    event_time = datetime.combine(
        session_date,
        time(20, 0),
        tzinfo=UTC,
    )
    values: dict[str, object] = {
        "symbol": "QQQ",
        "session_date": session_date,
        "volume": "100000",
    }
    return MarketBar(
        bar_id=f"q1-adv-bar-{index:02d}",
        provider=PROVIDER,
        feed=FEED,
        symbol="QQQ",
        timeframe="1Day",
        event_time=event_time,
        provider_timestamp=event_time.isoformat(),
        available_at=event_time + timedelta(hours=1),
        ingested_at=event_time + timedelta(hours=1),
        source_kind=MarketDataSourceKind.REST_BACKFILL,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("100000"),
        vwap=Decimal("100"),
        trade_count=1000,
        request_id="q1-adv-history",
        payload_hash=canonical_hash(values),
        raw_object_uri=None,
        payload={
            "_adjustment": "all",
            "_dataset_version": "alpaca_iex_adjusted_all_v1",
        },
    )


def _execution_quote() -> MarketQuote:
    event_time = EXECUTION_AT - timedelta(seconds=5)
    values = {
        "symbol": "QQQ",
        "event_time": event_time.isoformat(),
        "bid": "99.90",
        "ask": "100.10",
    }
    return MarketQuote(
        quote_id="q1-execution-quote",
        provider=PROVIDER,
        feed=FEED,
        symbol="QQQ",
        event_time=event_time,
        provider_timestamp=event_time.isoformat(),
        available_at=event_time,
        ingested_at=event_time,
        source_kind=MarketDataSourceKind.STREAM_QUOTE,
        bid_exchange="V",
        bid_price=Decimal("99.90"),
        bid_size_round_lots=1,
        ask_exchange="V",
        ask_price=Decimal("100.10"),
        ask_size_round_lots=1,
        conditions=[],
        tape="C",
        payload_hash=canonical_hash(values),
        raw_object_uri=None,
        payload=values,
    )


def _seed_executable_market(
    factory: sessionmaker[Session],
) -> None:
    market = MarketDataRepository(factory)
    market.append(
        bars=[_market_bar(index) for index in range(20)],
        quotes=[_execution_quote()],
    )
    market.ensure_status(
        provider=PROVIDER,
        feed=FEED,
        state=MarketConnectionState.CONNECTED,
        now=EXECUTION_AT,
    )
    market.transition(
        provider=PROVIDER,
        feed=FEED,
        state=MarketConnectionState.CONNECTED,
        now=EXECUTION_AT,
    )


def _execution_case(
    factory: sessionmaker[Session],
    *,
    repository_root: Path,
    valid_until: datetime,
    include_market: bool,
) -> _ExecutionCase:
    runtime = _runtime(factory, repository_root=repository_root)
    calendar = _register_calendars(
        runtime,
        include_next_session=True,
    )
    cycle = _seed_run_and_cycle(
        factory,
        cycle_id="q1-execution-cycle",
        cycle_kind="Q1_EXECUTION",
        scheduled_at=EXECUTION_AT,
    )
    intent = _seed_order_and_state(
        factory,
        runtime=runtime,
        cycle=cycle,
        valid_until=valid_until,
    )
    if include_market:
        _seed_executable_market(factory)
    return _ExecutionCase(
        runtime=runtime,
        processor=Q1ExecutionCycleProcessor(
            factory,
            runtime=runtime,
            workspace_root=repository_root,
        ),
        cycle=cycle,
        calendar=calendar,
        order_intent_id=intent.order_intent_id,
    )


def _row_counts(factory: sessionmaker[Session]) -> dict[str, int]:
    with factory() as session:
        return {
            "fills": session.scalar(
                select(func.count()).select_from(FillRow)
            )
            or 0,
            "order_events": session.scalar(
                select(func.count()).select_from(OrderEventRow)
            )
            or 0,
            "cash_events": session.scalar(
                select(func.count()).select_from(CashSettlementEventRow)
            )
            or 0,
            "states": session.scalar(
                select(func.count()).select_from(ArmStateSnapshotRow)
            )
            or 0,
        }


def test_execution_partial_fill_commits_order_state_and_sell_settlement(
    sqlite_database,
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    case = _execution_case(
        factory,
        repository_root=repository_root,
        valid_until=ORDER_CREATED_AT + timedelta(minutes=20),
        include_market=True,
    )

    output = case.processor.process(
        case.cycle,
        calendar=case.calendar,
        now=EXECUTION_AT,
    )

    with factory() as session:
        fill = session.scalar(select(FillRow))
        cash_event = session.scalar(select(CashSettlementEventRow))
        state = latest_arm_state(
            session,
            run_id=RUN_ID,
            arm_id=Q1ArmId.Q1_DET.value,
        )
        book = load_q1_order_book(session, run_id=RUN_ID)
        aggregate = pending_orders(book.descriptors, book.events)[0]
        cycle = session.get(PaperCycleRow, case.cycle.cycle_id)

    assert output["status"] == "Q1_EXECUTION_EVENTS_COMMITTED"
    assert output["real_order_routing"] is False
    assert fill is not None
    assert fill.quantity == Decimal("10")
    assert fill.effective_at.replace(tzinfo=UTC) == EXECUTION_AT
    assert aggregate.status is OrderEventType.PARTIALLY_FILLED
    assert aggregate.remaining_quantity == Decimal("90")
    assert aggregate.cumulative_filled_quantity == Decimal("10")
    assert aggregate.cumulative_commission_usd == fill.commission_usd
    assert state is not None
    assert state.sequence == 1
    assert state.positions == {"QQQ": Decimal("90")}
    assert state.settled_cash_usd == 0
    assert len(state.unsettled_receivables) == 1
    assert cash_event is not None
    assert (
        cash_event.event_type
        == CashSettlementEventType.SELL_RECEIVABLE_CREATED.value
    )
    assert cash_event.source_fill_id == fill.fill_id
    assert cash_event.settlement_date == SESSION_DATE + timedelta(days=1)
    assert (
        cash_event.unsettled_receivable_delta_usd
        == state.unsettled_receivables[0].amount_usd
    )
    assert (
        state.unsettled_receivables[0].amount_usd
        == fill.quantity * fill.price - fill.commission_usd
    )
    assert cycle is not None
    assert cycle.status == "COMPLETED"


def test_execution_retry_after_commit_cannot_duplicate_fill_or_events(
    sqlite_database,
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    case = _execution_case(
        factory,
        repository_root=repository_root,
        valid_until=ORDER_CREATED_AT + timedelta(minutes=20),
        include_market=True,
    )
    case.processor.process(
        case.cycle,
        calendar=case.calendar,
        now=EXECUTION_AT,
    )
    committed_counts = _row_counts(factory)

    with pytest.raises(Q1StaleWorkerError):
        case.processor.process(
            case.cycle,
            calendar=case.calendar,
            now=EXECUTION_AT + timedelta(seconds=1),
        )

    assert _row_counts(factory) == committed_counts
    assert committed_counts == {
        "fills": 1,
        "order_events": 2,
        "cash_events": 1,
        "states": 2,
    }


def test_execution_expiry_appends_an_explicit_terminal_event(
    sqlite_database,
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    case = _execution_case(
        factory,
        repository_root=repository_root,
        valid_until=ORDER_CREATED_AT + timedelta(minutes=1),
        include_market=False,
    )
    expiry_at = ORDER_CREATED_AT + timedelta(minutes=2)

    output = case.processor.process(
        case.cycle,
        calendar=case.calendar,
        now=expiry_at,
    )

    with factory() as session:
        book = load_q1_order_book(session, run_id=RUN_ID)
        events = tuple(
            event
            for event in book.events
            if event.order_intent_id == case.order_intent_id
        )
        fills = session.scalar(select(func.count()).select_from(FillRow))
        cash_events = session.scalar(
            select(func.count()).select_from(CashSettlementEventRow)
        )

    assert output["fill_ids"] == []
    assert [event.event_type for event in events] == [
        OrderEventType.CREATED,
        OrderEventType.EXPIRED,
    ]
    assert events[-1].remaining_quantity == Decimal("100")
    assert pending_orders(book.descriptors, book.events) == ()
    assert fills == 0
    assert cash_events == 0


def test_execution_at_actual_session_close_cannot_fill(
    sqlite_database,
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    case = _execution_case(
        factory,
        repository_root=repository_root,
        valid_until=SESSION_CLOSE + timedelta(minutes=5),
        include_market=True,
    )
    with factory.begin() as session:
        cycle = session.get(PaperCycleRow, case.cycle.cycle_id)
        assert cycle is not None
        cycle.lease_expires_at = SESSION_CLOSE + timedelta(minutes=1)

    output = case.processor.process(
        case.cycle,
        calendar=case.calendar,
        now=SESSION_CLOSE,
    )

    with factory() as session:
        fills = session.scalar(select(func.count()).select_from(FillRow))
        book = load_q1_order_book(session, run_id=RUN_ID)
        events = tuple(
            event
            for event in book.events
            if event.order_intent_id == case.order_intent_id
        )

    assert output["fill_ids"] == []
    assert fills == 0
    assert [event.event_type for event in events] == [
        OrderEventType.CREATED,
        OrderEventType.EXPIRED,
    ]


def test_execution_stale_lease_cannot_commit_prepared_mutations(
    sqlite_database,
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    case = _execution_case(
        factory,
        repository_root=repository_root,
        valid_until=ORDER_CREATED_AT + timedelta(minutes=20),
        include_market=True,
    )
    before = _row_counts(factory)
    with factory.begin() as session:
        reclaimed = session.get(PaperCycleRow, case.cycle.cycle_id)
        assert reclaimed is not None
        reclaimed.lease_owner = "replacement-worker"
        reclaimed.attempt_count = 2
        reclaimed.lease_expires_at = EXECUTION_AT + timedelta(hours=1)
    with factory() as session:
        replacement_cycle = session.get(PaperCycleRow, case.cycle.cycle_id)
        assert replacement_cycle is not None

    with pytest.raises(Q1StaleWorkerError):
        case.processor.process(
            case.cycle,
            calendar=case.calendar,
            now=EXECUTION_AT,
        )

    assert _row_counts(factory) == before
    assert before == {
        "fills": 0,
        "order_events": 1,
        "cash_events": 0,
        "states": 1,
    }

    output = case.processor.process(
        replacement_cycle,
        calendar=case.calendar,
        now=EXECUTION_AT,
    )

    assert output["status"] == "Q1_EXECUTION_EVENTS_COMMITTED"
    assert _row_counts(factory) == {
        "fills": 1,
        "order_events": 2,
        "cash_events": 1,
        "states": 2,
    }


def _evaluation_case(
    factory: sessionmaker[Session],
    *,
    repository_root: Path,
) -> tuple[
    Q1EvaluationCycleProcessor,
    PaperCycleRow,
    VersionedMarketSession,
    StrategyEvaluationAnchor,
]:
    runtime = _runtime(factory, repository_root=repository_root)
    calendar = _register_calendars(
        runtime,
        include_next_session=False,
    )
    cycle = _seed_run_and_cycle(
        factory,
        cycle_id="q1-evaluation-cycle",
        cycle_kind="Q1_DAILY_RESULT",
        scheduled_at=SESSION_CLOSE,
    )
    anchor_values = {
        "run_id": RUN_ID,
        "calendar_session_id": calendar.calendar_session_id,
        "common_t0_at": ORDER_CREATED_AT,
        "initial_nav_usd": Decimal("10000"),
        "quote_manifest_hash": HASH,
        "config_manifest_hash": runtime.config.manifest_hash,
        "code_version": "test-code",
        "model_version": Q1_MODEL_VERSION,
        "source_manifest_hash": HASH,
    }
    anchor = StrategyEvaluationAnchor(
        evaluation_anchor_id=ANCHOR_ID,
        anchor_hash=canonical_hash(anchor_values),
        created_at=ORDER_CREATED_AT,
        **anchor_values,
    )
    with factory.begin() as session:
        StrategyEvaluationAnchorRepository(session).append(anchor)
        for arm_id in Q1ArmId:
            append_arm_state(
                session,
                run_id=RUN_ID,
                state=Q1ArmState(
                    arm_id=arm_id.value,
                    initial_nav_usd=Decimal("10000"),
                    settled_cash_usd=Decimal("10000"),
                    unsettled_receivables=(),
                    positions={},
                    sequence=0,
                    evaluation_anchor_id=ANCHOR_ID,
                ),
                source_cycle_id=cycle.cycle_id,
                created_at=ORDER_CREATED_AT,
                expected_previous_sequence=None,
            )
    return (
        Q1EvaluationCycleProcessor(
            factory,
            runtime=runtime,
            workspace_root=repository_root,
        ),
        cycle,
        calendar,
        anchor,
    )


def test_evaluation_commits_all_arms_exact_matched_pairs_and_deterministic_bootstrap(
    sqlite_database,
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    processor, cycle, calendar, anchor = _evaluation_case(
        factory,
        repository_root=repository_root,
    )
    evaluated_at = SESSION_CLOSE + timedelta(seconds=1)

    output = processor.process(
        cycle,
        calendar=calendar,
        now=evaluated_at,
    )

    with factory() as session:
        daily = tuple(
            session.scalars(
                select(StrategyDailyResultRow).order_by(
                    StrategyDailyResultRow.arm_id
                )
            )
        )
        matched = tuple(
            session.scalars(
                select(MatchedAttributionResultRow).order_by(
                    MatchedAttributionResultRow.comparison
                )
            )
        )
    observations = processor._observations_with_prepared(
        run_id=RUN_ID,
        prepared={},
    )
    recomputed_once = processor._prepare_matched(
        cycle=cycle,
        calendar=calendar,
        anchor=anchor,
        observations=observations,
        now=evaluated_at,
        code_version=matched[0].code_version,
        source_manifest_hash=matched[0].source_manifest_hash,
    )
    recomputed_twice = processor._prepare_matched(
        cycle=cycle,
        calendar=calendar,
        anchor=anchor,
        observations=observations,
        now=evaluated_at,
        code_version=matched[0].code_version,
        source_manifest_hash=matched[0].source_manifest_hash,
    )

    assert output["status"] == "Q1_DAILY_RESULTS_COMMITTED"
    assert output["real_order_routing"] is False
    assert {row.arm_id for row in daily} == {
        arm_id.value for arm_id in Q1ArmId
    }
    assert len(daily) == len(Q1ArmId)
    assert all(row.nav_usd == Decimal("10000") for row in daily)
    assert all(row.net_daily_return == 0 for row in daily)
    assert {
        (
            row.comparison,
            row.left_arm_id,
            row.right_arm_id,
        )
        for row in matched
    } == {
        (
            MatchedComparison.Q1_DET_MINUS_B0_VOL.value,
            Q1ArmId.Q1_DET.value,
            Q1ArmId.B0_VOL.value,
        ),
        (
            MatchedComparison.Q1_LLM_MINUS_Q1_DET.value,
            Q1ArmId.Q1_LLM.value,
            Q1ArmId.Q1_DET.value,
        ),
    }
    assert len(matched) == 2
    assert all(row.bootstrap_seed == 7077 for row in matched)
    assert all(row.common_valid_sessions == 1 for row in matched)
    assert all(row.bootstrap_lower == 0 for row in matched)
    assert all(row.bootstrap_upper == 0 for row in matched)
    assert recomputed_once == recomputed_twice
    assert {
        item.result_hash for item in recomputed_once
    } == {
        row.result_hash for row in matched
    }
