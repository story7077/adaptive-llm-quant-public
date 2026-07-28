from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from trading.data.alpaca import (
    FEED,
    PROVIDER,
    parse_rest_bar,
    parse_stream_message,
)
from trading.data.market_repository import MarketDataRepository
from trading.domain.contracts import (
    Fill,
    MarketQuote,
    OrderIntent,
    PortfolioDecision,
    model_payload,
)
from trading.domain.enums import MarketConnectionState, OrderSide
from trading.domain.hashing import canonical_hash, stable_id
from trading.execution.live_paper import QuoteDrivenFill
from trading.experiments.arms import ArmState
from trading.persistence.models import (
    ArmStateSnapshotRow,
    FillRow,
    ForwardStrategyCandidateRow,
    LedgerTransactionRow,
    NavSnapshotRow,
    OrderIntentRow,
    PaperCycleEffectRow,
    PaperCycleRow,
    PaperExecutionAttemptRow,
    PortfolioDecisionRow,
    RunRow,
    ShadowArmRow,
)
from trading.runtime.forward_paper import (
    ForwardPaperConflict,
    ForwardPaperTradingService,
    PendingOrder,
    PreparedFill,
)
from trading.runtime.paper import PaperRuntimeService
from trading.runtime.scheduler import PaperCycleSlot, PaperCycleStore

POSITION_PRICES = {
    "SPY": Decimal("650"),
    "QQQ": Decimal("560"),
    "IWM": Decimal("240"),
    "SMH": Decimal("310"),
    "TLT": Decimal("90"),
    "HYG": Decimal("80"),
    "GLD": Decimal("320"),
}
ALL_SYMBOLS = tuple(POSITION_PRICES)
NEW_YORK = ZoneInfo("America/New_York")


def test_forward_baseline_decision_to_quote_fill_is_atomic_and_idempotent(
    sqlite_database,
    config_bundle,
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    repository = MarketDataRepository(factory)
    paper = PaperRuntimeService(
        factory,
        config=config_bundle,
        workspace_root=repository_root,
    )
    forward = ForwardPaperTradingService(
        factory,
        config=config_bundle,
        max_quote_age_seconds=15,
    )
    cycles = PaperCycleStore(factory)
    run_id = "forward-orders"
    market_open = datetime(2026, 7, 28, 13, 30, tzinfo=UTC)
    decision_time = datetime(2026, 7, 28, 19, 45, tzinfo=UTC)
    execution_time = decision_time + timedelta(minutes=1)
    account_file = repository_root / "config" / "paper-account.example.yaml"

    paper.initialize(
        run_id=run_id,
        account_file=account_file,
        now=market_open - timedelta(minutes=5),
    )
    repository.append(
        quotes=[
            _quote(
                symbol,
                price,
                event_time=market_open,
                available_at=market_open + timedelta(seconds=1),
            )
            for symbol, price in POSITION_PRICES.items()
        ]
    )
    paper.bootstrap_from_fresh_quotes(
        run_id=run_id,
        session_open_at=market_open,
        account_file=account_file,
        max_quote_age_seconds=15,
        now=market_open + timedelta(seconds=5),
    )
    repository.append(bars=_daily_bars(decision_time))
    repository.append(
        bars=[
            parse_rest_bar(
                symbol="QQQ",
                payload={
                    "t": "2026-07-28T04:00:00Z",
                    "o": "1",
                    "h": "10000",
                    "l": "1",
                    "c": "10000",
                    "v": "999999999",
                    "vw": "5000",
                    "n": 999999,
                },
                available_at=decision_time - timedelta(minutes=15),
                raw_object_uri="raw://daily/QQQ/current-partial",
                request_id="daily-current-partial",
                timeframe="1Day",
                adjustment="all",
            )
        ]
    )
    pit_rows = forward._qqq_daily_rows(decision_time)
    assert all(
        (
            row.event_time.replace(tzinfo=UTC)
            if row.event_time.tzinfo is None
            else row.event_time.astimezone(UTC)
        )
        < datetime(2026, 7, 28, 4, tzinfo=UTC)
        for row in pit_rows
    )
    assert forward._adv_fill_cap(
        "QQQ",
        run_id=run_id,
        arm_id="B3-RISK",
        as_of=decision_time,
    ) == Decimal("2500.000000")
    repository.append(
        quotes=[
            _quote(
                symbol,
                POSITION_PRICES.get(symbol, Decimal("560")),
                event_time=decision_time - timedelta(seconds=2),
                available_at=decision_time - timedelta(seconds=1),
            )
            for symbol in ALL_SYMBOLS
        ]
    )
    repository.ensure_status(
        provider=PROVIDER,
        feed=FEED,
        state=MarketConnectionState.CONNECTED,
        now=decision_time - timedelta(seconds=1),
    )
    cycles.ensure_slots(
        run_id=run_id,
        slots=(
            PaperCycleSlot("DECISION", decision_time),
            PaperCycleSlot("EXECUTION", execution_time),
        ),
        now=market_open,
    )
    decision_cycle = cycles.claim_next(
        run_id=run_id,
        now=decision_time + timedelta(seconds=1),
        grace=timedelta(minutes=5),
        owner="decision-worker",
        kinds=frozenset({"DECISION"}),
    )
    assert decision_cycle is not None
    decision_output = forward.decide(
        decision_cycle,
        run_id=run_id,
        data_available_cutoff=decision_time,
        created_at=decision_time + timedelta(seconds=2),
    )
    assert decision_output["orders_created"] > 0
    assert decision_output["signal_data_as_of"] == "2026-07-28T19:45:00Z"
    assert decision_output["policy_as_of"] == "2026-07-28T19:45:00Z"
    assert (
        decision_output["portfolio_and_quotes_as_of"]
        == "2026-07-28T19:45:02Z"
    )
    cycles.complete(
        decision_cycle.cycle_id,
        lease_owner="decision-worker",
        attempt_count=decision_cycle.attempt_count,
        cutoff=decision_time,
        input_manifest={"cycle": "decision"},
        output_manifest=decision_output,
        now=decision_time + timedelta(seconds=3),
    )

    with factory() as session:
        intents = list(
            session.scalars(
                select(OrderIntentRow).where(OrderIntentRow.run_id == run_id)
            )
        )
        assert intents
        assert all(row.symbol != "SOXS" for row in intents)
        assert all(not (row.symbol == "SOXL" and row.side == "BUY") for row in intents)
        assert session.scalar(
            select(func.count()).select_from(ForwardStrategyCandidateRow)
        ) == 3

    repository.append(
        quotes=[
            _quote(
                symbol,
                POSITION_PRICES.get(symbol, Decimal("560")),
                event_time=execution_time - timedelta(seconds=5),
                available_at=execution_time - timedelta(seconds=4),
            )
            for symbol in ALL_SYMBOLS
        ]
    )
    execution_cycle = cycles.claim_next(
        run_id=run_id,
        now=execution_time,
        grace=timedelta(minutes=5),
        owner="execution-worker",
        kinds=frozenset({"EXECUTION"}),
    )
    assert execution_cycle is not None
    first = forward.execute(
        execution_cycle,
        run_id=run_id,
        now=execution_time,
    )
    replay = forward.execute(
        execution_cycle,
        run_id=run_id,
        now=execution_time + timedelta(seconds=1),
    )
    assert first == replay
    assert first["fills_created"] > 0, [
        item["status"] for item in first["attempts"]
    ]
    cycles.complete(
        execution_cycle.cycle_id,
        lease_owner="execution-worker",
        attempt_count=execution_cycle.attempt_count,
        cutoff=execution_time,
        input_manifest={"cycle": "execution"},
        output_manifest=first,
        now=execution_time + timedelta(seconds=2),
    )

    with factory() as session:
        fill_count = session.scalar(
            select(func.count()).select_from(FillRow).where(FillRow.run_id == run_id)
        )
        assert fill_count == first["fills_created"]
        assert session.scalar(
            select(func.count())
            .select_from(LedgerTransactionRow)
            .where(
                LedgerTransactionRow.run_id == run_id,
                LedgerTransactionRow.source_id.in_(
                    select(FillRow.fill_id).where(FillRow.run_id == run_id)
                ),
            )
        ) == fill_count
        assert session.scalar(
            select(func.count())
            .select_from(ArmStateSnapshotRow)
            .where(
                ArmStateSnapshotRow.run_id == run_id,
                ArmStateSnapshotRow.source_cycle_id == execution_cycle.cycle_id,
            )
        ) == fill_count
        assert session.scalar(
            select(func.count())
            .select_from(NavSnapshotRow)
            .where(
                NavSnapshotRow.run_id == run_id,
                NavSnapshotRow.source_cycle_id == execution_cycle.cycle_id,
            )
        ) > 0
        assert session.scalar(
            select(func.count())
            .select_from(PaperCycleEffectRow)
            .where(PaperCycleEffectRow.run_id == run_id)
        ) == 2
        fills = list(
            session.scalars(select(FillRow).where(FillRow.run_id == run_id))
        )
        assert all(row.quote_id is not None for row in fills)
        assert all(
            row.quote_available_at is not None
            and (
                row.quote_available_at.replace(tzinfo=UTC)
                if row.quote_available_at.tzinfo is None
                else row.quote_available_at
            )
            > decision_time
            for row in fills
        )
        consumed_arm = fills[0].arm_id
        consumed_symbol = str(fills[0].symbol)
        consumed_quantity = Decimal(fills[0].quantity or 0)

    assert forward._adv_fill_cap(
        consumed_symbol,
        run_id=run_id,
        arm_id=consumed_arm,
        as_of=execution_time + timedelta(seconds=1),
    ) == Decimal("2500.000000") - consumed_quantity

    status = paper.status(run_id)
    assert status["order_generation_enabled"] is True
    assert status["real_order_routing"] is False
    assert status["arms"]["B3-RISK"]["sequence"] > 0
    assert status["arms"]["HOLD"]["sequence"] == 0
    assert status["arms"]["HOLD"]["orders"] == []


@pytest.mark.parametrize(
    "terminal_status",
    [
        "LOSS_GUARD_BLOCKED_PENDING_BUY",
        "SUPERSEDED_BY_NEWER_PORTFOLIO_DECISION",
    ],
)
def test_execution_rejects_terminal_attempt_committed_after_preparation(
    sqlite_database,
    config_bundle,
    terminal_status: str,
) -> None:
    _, _, factory = sqlite_database
    run_id = f"terminal-race-{terminal_status}"
    arm_id = "B3-RISK"
    decision_time = datetime(2026, 7, 28, 19, 45, tzinfo=UTC)
    execution_time = decision_time + timedelta(minutes=1)
    decision_id = stable_id("portfolio-decision", run_id, decision_time)
    portfolio_decision = _portfolio_decision(
        decision_id=decision_id,
        arm_id=arm_id,
        decision_time=decision_time,
        created_at=decision_time,
    )
    intent_id = stable_id("order-intent", run_id, decision_time)
    state = ArmState(
        arm_id=arm_id,
        initial_cash_usd=Decimal("1000"),
        cash_usd=Decimal("1000"),
        positions={},
        sequence=0,
    )
    intent = OrderIntent(
        order_intent_id=intent_id,
        arm_id=arm_id,
        portfolio_decision_id=decision_id,
        risk_decision_id=stable_id("risk-decision", decision_id),
        symbol="QQQ",
        side=OrderSide.BUY,
        order_type="MARKET",
        quantity=Decimal("1"),
        limit_price=None,
        time_in_force="DAY",
        session="REGULAR",
        client_order_id=stable_id("client-order", intent_id),
        idempotency_key=stable_id("idempotency", intent_id),
        created_at=decision_time,
    )
    fill = Fill(
        fill_id=stable_id("fill", intent_id, execution_time),
        order_intent_id=intent_id,
        arm_id=arm_id,
        symbol="QQQ",
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        commission_usd=Decimal("0.1"),
        execution_scenario_id="race-test",
        effective_at=execution_time,
        created_at=execution_time,
    )
    cycles = PaperCycleStore(factory)
    with factory.begin() as session:
        session.add(
            RunRow(
                run_id=run_id,
                mode="PAPER",
                experiment_version="test",
                config_manifest_hash=config_bundle.manifest_hash,
                code_commit="test",
                started_at=decision_time,
                ended_at=None,
                status="RUNNING",
                result_manifest={},
                result_hash=None,
            )
        )
    cycles.ensure_slots(
        run_id=run_id,
        slots=(
            PaperCycleSlot("DECISION", decision_time),
            PaperCycleSlot("EXECUTION", execution_time),
        ),
        now=decision_time,
    )
    with factory() as session:
        decision_cycle = session.scalar(
            select(PaperCycleRow).where(
                PaperCycleRow.run_id == run_id,
                PaperCycleRow.cycle_kind == "DECISION",
            )
        )
    assert decision_cycle is not None
    with factory.begin() as session:
        session.add(
            ShadowArmRow(
                arm_instance_id=stable_id("arm", run_id, arm_id),
                run_id=run_id,
                arm_id=arm_id,
                created_at=decision_time,
            )
        )
        session.add(
            ArmStateSnapshotRow(
                arm_state_snapshot_id=stable_id("arm-state", run_id, arm_id, 0),
                run_id=run_id,
                arm_id=arm_id,
                sequence=0,
                source_cycle_id=None,
                state_hash=canonical_hash(state.as_payload()),
                payload_json=state.as_payload(),
                created_at=decision_time,
            )
        )
        session.add(
            PortfolioDecisionRow(
                portfolio_decision_id=decision_id,
                run_id=run_id,
                arm_id=arm_id,
                source_cycle_id=decision_cycle.cycle_id,
                input_state_sequence=0,
                decision_time=decision_time,
                payload_json=model_payload(portfolio_decision),
                decision_hash=canonical_hash(portfolio_decision),
            )
        )
        intent_row = OrderIntentRow(
            order_intent_id=intent_id,
            run_id=run_id,
            arm_id=arm_id,
            source_cycle_id=decision_cycle.cycle_id,
            input_state_sequence=0,
            symbol=intent.symbol,
            side=intent.side.value,
            quantity=intent.quantity,
            created_at=intent.created_at,
            valid_until=execution_time + timedelta(minutes=30),
            decision_quote_id="decision-quote",
            decision_reference_price=Decimal("100"),
            idempotency_key=intent.idempotency_key,
            payload_json=model_payload(intent),
            intent_hash=canonical_hash(intent),
        )
        session.add(intent_row)

    execution_cycle = cycles.claim_next(
        run_id=run_id,
        now=execution_time,
        grace=timedelta(minutes=5),
        owner="execution-worker",
        kinds=frozenset({"EXECUTION"}),
    )
    assert execution_cycle is not None
    pending = PendingOrder(
        row=intent_row,
        intent=intent,
        remaining_quantity=intent.quantity,
        cumulative_notional_usd=Decimal("0"),
        cumulative_commission_usd=Decimal("0"),
        observed_after=intent.created_at,
    )
    prepared = PreparedFill(
        pending=pending,
        driven=QuoteDrivenFill(
            fill=fill,
            quote_id="execution-quote",
            quote_event_time=execution_time,
            quote_available_at=execution_time,
        ),
        state_before_sequence=state.sequence,
        state_after=state.apply_fill(fill),
    )

    terminal_payload = {
        "order_intent_id": intent_id,
        "status": terminal_status,
    }
    with factory.begin() as session:
        session.add(
            PaperExecutionAttemptRow(
                attempt_id=stable_id(
                    "paper-execution-attempt",
                    decision_cycle.cycle_id,
                    intent_id,
                ),
                cycle_id=decision_cycle.cycle_id,
                order_intent_id=intent_id,
                quote_id=None,
                status=terminal_status,
                remaining_quantity_before=intent.quantity,
                remaining_quantity_after=intent.quantity,
                cumulative_notional_usd=Decimal("0"),
                cumulative_commission_usd=Decimal("0"),
                attempt_hash=canonical_hash(terminal_payload),
                payload_json=terminal_payload,
                created_at=execution_time,
            )
        )

    forward = ForwardPaperTradingService(
        factory,
        config=config_bundle,
        max_quote_age_seconds=15,
    )
    with pytest.raises(
        ForwardPaperConflict,
        match="terminally canceled during execution preparation",
    ):
        forward._persist_execution(
            cycle=execution_cycle,
            run_id=run_id,
            now=execution_time + timedelta(seconds=1),
            input_manifest={"cycle": "execution"},
            pending=[pending],
            initial_states={arm_id: state},
            prepared=[prepared],
            nav_inputs={},
            attempt_payloads=[
                {
                    "order_intent_id": intent_id,
                    "status": "FILLED",
                }
            ],
            output={
                "status": "FORWARD_FILLS_COMMITTED",
                "fills_created": 1,
            },
        )

    with factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(FillRow)
            .where(FillRow.run_id == run_id)
        ) == 0
        assert session.scalar(
            select(func.count())
            .select_from(PaperCycleEffectRow)
            .where(PaperCycleEffectRow.cycle_id == execution_cycle.cycle_id)
        ) == 0


def test_same_time_forward_decisions_use_actual_creation_order(
    sqlite_database,
    config_bundle,
) -> None:
    _, _, factory = sqlite_database
    run_id = "same-time-decision-order"
    arm_id = "B3-RISK"
    scheduled_at = datetime(2026, 7, 28, 18, 0, tzinfo=UTC)
    cycles = PaperCycleStore(factory)
    with factory.begin() as session:
        session.add(
            RunRow(
                run_id=run_id,
                mode="PAPER",
                experiment_version="test",
                config_manifest_hash=config_bundle.manifest_hash,
                code_commit="test",
                started_at=scheduled_at,
                ended_at=None,
                status="RUNNING",
                result_manifest={},
                result_hash=None,
            )
        )
    cycles.ensure_slots(
        run_id=run_id,
        slots=(
            PaperCycleSlot("NAV", scheduled_at),
            PaperCycleSlot("DECISION", scheduled_at),
        ),
        now=scheduled_at,
    )
    with factory() as session:
        cycle_rows = list(
            session.scalars(
                select(PaperCycleRow).where(PaperCycleRow.run_id == run_id)
            )
        )
    cycle_by_kind = {row.cycle_kind: row for row in cycle_rows}
    older = _portfolio_decision(
        decision_id="zzz-older-decision",
        arm_id=arm_id,
        decision_time=scheduled_at,
        created_at=scheduled_at + timedelta(seconds=1),
    )
    newer = _portfolio_decision(
        decision_id="aaa-newer-decision",
        arm_id=arm_id,
        decision_time=scheduled_at,
        created_at=scheduled_at + timedelta(seconds=10),
    )
    with factory.begin() as session:
        for decision, cycle_kind in ((older, "NAV"), (newer, "DECISION")):
            session.add(
                PortfolioDecisionRow(
                    portfolio_decision_id=decision.portfolio_decision_id,
                    run_id=run_id,
                    arm_id=arm_id,
                    source_cycle_id=cycle_by_kind[cycle_kind].cycle_id,
                    input_state_sequence=0,
                    decision_time=decision.decision_time,
                    payload_json=model_payload(decision),
                    decision_hash=canonical_hash(decision),
                )
            )
    with factory() as session:
        assert (
            ForwardPaperTradingService._latest_forward_decision_id(
                session,
                run_id=run_id,
                arm_id=arm_id,
            )
            == newer.portfolio_decision_id
        )


def test_forward_state_lookup_excludes_snapshots_created_after_actual_time(
    sqlite_database,
    config_bundle,
) -> None:
    _, _, factory = sqlite_database
    run_id = "state-pit-cutoff"
    arm_id = "B3-RISK"
    actual = datetime(2026, 7, 28, 19, 45, 2, tzinfo=UTC)
    state_at_actual = ArmState(
        arm_id=arm_id,
        initial_cash_usd=Decimal("1000"),
        cash_usd=Decimal("1000"),
        positions={},
        sequence=0,
    )
    future_state = ArmState(
        arm_id=arm_id,
        initial_cash_usd=Decimal("1000"),
        cash_usd=Decimal("900"),
        positions={"QQQ": Decimal("0.2")},
        sequence=1,
    )
    with factory.begin() as session:
        session.add(
            RunRow(
                run_id=run_id,
                mode="PAPER",
                experiment_version="test",
                config_manifest_hash=config_bundle.manifest_hash,
                code_commit="test",
                started_at=actual - timedelta(minutes=1),
                ended_at=None,
                status="RUNNING",
                result_manifest={},
                result_hash=None,
            )
        )
        session.flush()
        for state, created_at in (
            (state_at_actual, actual),
            (future_state, actual + timedelta(seconds=1)),
        ):
            session.add(
                ArmStateSnapshotRow(
                    arm_state_snapshot_id=stable_id(
                        "arm-state",
                        run_id,
                        arm_id,
                        state.sequence,
                    ),
                    run_id=run_id,
                    arm_id=arm_id,
                    sequence=state.sequence,
                    source_cycle_id=None,
                    state_hash=canonical_hash(state.as_payload()),
                    payload_json=state.as_payload(),
                    created_at=created_at,
                )
            )
    forward = ForwardPaperTradingService(
        factory,
        config=config_bundle,
        max_quote_age_seconds=15,
    )

    selected = forward._latest_states(
        run_id,
        (arm_id,),
        as_of=actual,
    )

    assert selected[arm_id].sequence == 0


def _quote(
    symbol: str,
    midpoint: Decimal,
    *,
    event_time: datetime,
    available_at: datetime,
) -> MarketQuote:
    event = parse_stream_message(
        {
            "T": "q",
            "S": symbol,
            "bx": "V",
            "bp": str(midpoint - Decimal("0.02")),
            "bs": 10,
            "ax": "V",
            "ap": str(midpoint + Decimal("0.02")),
            "as": 10,
            "c": [],
            "t": event_time.isoformat().replace("+00:00", "Z"),
            "z": "C",
        },
        available_at=available_at,
        raw_object_uri=f"raw://quote/{symbol}/{available_at.timestamp()}",
    )
    assert isinstance(event, MarketQuote)
    return event


def _portfolio_decision(
    *,
    decision_id: str,
    arm_id: str,
    decision_time: datetime,
    created_at: datetime,
) -> PortfolioDecision:
    return PortfolioDecision(
        portfolio_decision_id=decision_id,
        arm_id=arm_id,
        decision_time=decision_time,
        core_portfolio_version="test-core",
        policy_version=0,
        forecast_ids=[],
        input_snapshot_hash="a" * 64,
        previous_weights={"USD_CASH": 1.0},
        target_weights_pre_risk={"QQQ": 1.0},
        expected_net_return_bps=0.0,
        expected_annualized_vol=0.0,
        expected_cvar_975=0.0,
        expected_turnover=0.0,
        expected_cost_usd=Decimal("0"),
        optimizer_status="TEST",
        solver_name="TEST",
        solver_diagnostics={},
        created_at=created_at,
    )


def _daily_bars(available_at: datetime):
    sessions: list[date] = []
    cursor = date(2026, 7, 27)
    while len(sessions) < 21:
        if cursor.weekday() < 5:
            sessions.append(cursor)
        cursor -= timedelta(days=1)
    sessions.reverse()
    bars = []
    for symbol in ALL_SYMBOLS:
        base = POSITION_PRICES.get(symbol, Decimal("500"))
        for index, session_date in enumerate(sessions):
            close = (
                base
                * (Decimal("1") + Decimal(index) / Decimal("1000"))
                * (Decimal("1.01") if index % 2 else Decimal("0.99"))
            )
            timestamp = datetime.combine(
                session_date,
                time.min,
                tzinfo=NEW_YORK,
            ).astimezone(UTC)
            bars.append(
                parse_rest_bar(
                    symbol=symbol,
                    payload={
                        "t": timestamp.isoformat().replace("+00:00", "Z"),
                        "o": str(close * Decimal("0.999")),
                        "h": str(close * Decimal("1.002")),
                        "l": str(close * Decimal("0.998")),
                        "c": str(close),
                        "v": "100000",
                        "vw": str(close),
                        "n": 1000,
                    },
                    available_at=available_at - timedelta(hours=1),
                    raw_object_uri=f"raw://daily/{symbol}/{session_date}",
                    request_id="daily-test",
                    timeframe="1Day",
                    adjustment="all",
                )
            )
    return bars
