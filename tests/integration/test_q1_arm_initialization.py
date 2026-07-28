# pyright: reportPrivateUsage=false

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import JsonValue
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import FEED, PROVIDER
from trading.data.market_repository import MarketDataRepository
from trading.domain.contracts import MarketQuote
from trading.domain.enums import MarketDataSourceKind
from trading.domain.hashing import canonical_hash
from trading.domain.q1 import Q1ArmId, StrategyEvaluationAnchor
from trading.domain.time import FrozenClock
from trading.persistence.models import NavSnapshotRow, PaperCycleRow
from trading.persistence.q1 import StrategyEvaluationAnchorRepository
from trading.persistence.q1_runtime import latest_arm_state
from trading.runtime.q1_cycle import STRATEGY_ARMS, Q1PaperCycleProcessor
from trading.runtime.q1_paper import Q1PaperRuntimeService
from trading.runtime.q1_scheduler import VersionedMarketSession
from trading.settings import load_q1_config_bundle

RUN_ID = "q1-arm-initialization"
OPEN_AT = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
BOOTSTRAP_AT = OPEN_AT + timedelta(seconds=5)
T0_AT = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
HASH = "a" * 64


def test_q1_arms_use_inherited_and_cash_only_initialization_contract(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    config = load_q1_config_bundle(repository_root / "config")
    runtime = Q1PaperRuntimeService(
        factory,
        config=config,
        workspace_root=repository_root,
        clock=FrozenClock(BOOTSTRAP_AT),
    )
    runtime.initialize(
        run_id=RUN_ID,
        account_file=repository_root / "config" / "paper-account.example.yaml",
    )
    calendar = VersionedMarketSession(
        calendar_session_id="q1-arm-calendar",
        calendar_version="alpaca_market_calendar_v1",
        session_date=date(2026, 7, 27),
        open_at=OPEN_AT,
        close_at=datetime(2026, 7, 27, 20, 0, tzinfo=UTC),
        source_payload_hash=HASH,
        source_available_at=OPEN_AT - timedelta(days=7),
    )
    runtime.register_calendar_session(
        calendar,
        now=OPEN_AT - timedelta(days=1),
    )
    bootstrap_cycle = _cycle(
        cycle_id="q1-arm-bootstrap",
        kind="Q1_BOOTSTRAP",
        scheduled_at=OPEN_AT,
    )
    with factory.begin() as session:
        session.add(bootstrap_cycle)
    inherited_symbols = (
        "SPY",
        "QQQ",
        "IWM",
        "SMH",
        "TLT",
        "HYG",
        "GLD",
    )
    MarketDataRepository(factory).append(
        quotes=[
            _quote(symbol, index)
            for index, symbol in enumerate(inherited_symbols, start=1)
        ]
    )
    Q1PaperCycleProcessor(
        factory,
        runtime=runtime,
        account_file=repository_root / "config" / "paper-account.example.yaml",
        workspace_root=repository_root,
        clock=FrozenClock(BOOTSTRAP_AT),
    ).process(bootstrap_cycle)

    with factory() as session:
        hold = latest_arm_state(
            session,
            run_id=RUN_ID,
            arm_id=Q1ArmId.HOLD.value,
        )
        live = latest_arm_state(
            session,
            run_id=RUN_ID,
            arm_id=Q1ArmId.LIVE_MIRROR.value,
        )
        opening_navs = tuple(
            session.scalars(
                select(NavSnapshotRow).where(
                    NavSnapshotRow.run_id == RUN_ID,
                    NavSnapshotRow.source_cycle_id
                    == bootstrap_cycle.cycle_id,
                )
            )
        )
    assert hold is not None and live is not None
    assert hold.positions == live.positions
    assert set(hold.positions) == set(inherited_symbols)
    assert {row.arm_id for row in opening_navs} == {
        Q1ArmId.HOLD.value,
        Q1ArmId.LIVE_MIRROR.value,
    }
    assert all(
        row.payload_json["session_open_baseline"] is True
        for row in opening_navs
    )

    anchor_nav = hold.initial_nav_usd
    anchor = StrategyEvaluationAnchor(
        evaluation_anchor_id="q1-common-anchor",
        run_id=RUN_ID,
        calendar_session_id=calendar.calendar_session_id,
        common_t0_at=T0_AT,
        initial_nav_usd=anchor_nav,
        quote_manifest_hash=HASH,
        anchor_hash=canonical_hash(
            {"run_id": RUN_ID, "common_t0_at": T0_AT}
        ),
        created_at=T0_AT,
        algorithm_version="q1_math_core_v1",
        config_manifest_hash=config.manifest_hash,
        code_version="test-code",
        model_version="test-model",
        source_manifest_hash=HASH,
    )
    strategic_cycle = _cycle(
        cycle_id="q1-arm-strategic",
        kind="Q1_STRATEGIC",
        scheduled_at=T0_AT,
    )
    processor = Q1PaperCycleProcessor(
        factory,
        runtime=runtime,
        account_file=repository_root / "config" / "paper-account.example.yaml",
        workspace_root=repository_root,
        clock=FrozenClock(T0_AT),
    )
    with factory.begin() as session:
        session.add(strategic_cycle)
        session.flush()
        StrategyEvaluationAnchorRepository(session).append(anchor)
        processor._append_strategy_opening_states(
            session,
            cycle=strategic_cycle,
            anchor=anchor,
            created_at=T0_AT,
            source_manifest_hash=HASH,
        )

    with factory() as session:
        strategy_states = {
            arm_id: latest_arm_state(
                session,
                run_id=RUN_ID,
                arm_id=arm_id.value,
            )
            for arm_id in STRATEGY_ARMS
        }
        strategy_navs = tuple(
            session.scalars(
                select(NavSnapshotRow).where(
                    NavSnapshotRow.run_id == RUN_ID,
                    NavSnapshotRow.source_cycle_id
                    == strategic_cycle.cycle_id,
                )
            )
        )

    assert all(state is not None for state in strategy_states.values())
    assert all(
        state is not None
        and state.positions == {}
        and state.settled_cash_usd == anchor_nav
        and state.evaluation_anchor_id == anchor.evaluation_anchor_id
        for state in strategy_states.values()
    )
    assert {row.arm_id for row in strategy_navs} == {
        arm_id.value
        for arm_id in STRATEGY_ARMS
    }
    assert all(row.nav_usd == anchor_nav for row in strategy_navs)

    next_open = OPEN_AT + timedelta(days=1)
    next_bootstrap_at = next_open + timedelta(seconds=5)
    next_calendar = VersionedMarketSession(
        calendar_session_id="q1-arm-calendar-next",
        calendar_version="alpaca_market_calendar_v1",
        session_date=date(2026, 7, 28),
        open_at=next_open,
        close_at=next_open + timedelta(hours=6, minutes=30),
        source_payload_hash="b" * 64,
        source_available_at=OPEN_AT,
    )
    runtime.register_calendar_session(
        next_calendar,
        now=OPEN_AT,
    )
    next_bootstrap = _cycle(
        cycle_id="q1-arm-bootstrap-next",
        kind="Q1_BOOTSTRAP",
        scheduled_at=next_open,
    )
    next_bootstrap.lease_expires_at = next_open + timedelta(hours=1)
    next_bootstrap.started_at = next_open
    next_bootstrap.created_at = next_open
    next_bootstrap.updated_at = next_open
    with factory.begin() as session:
        session.add(next_bootstrap)
    MarketDataRepository(factory).append(
        quotes=[
            _quote(
                symbol,
                index,
                event_time=next_open + timedelta(seconds=1),
                quote_prefix="q1-arm-next",
            )
            for index, symbol in enumerate(inherited_symbols, start=1)
        ]
    )
    Q1PaperCycleProcessor(
        factory,
        runtime=runtime,
        account_file=repository_root / "config" / "paper-account.example.yaml",
        workspace_root=repository_root,
        clock=FrozenClock(next_bootstrap_at),
    ).process(next_bootstrap)

    with factory() as session:
        next_open_navs = tuple(
            session.scalars(
                select(NavSnapshotRow).where(
                    NavSnapshotRow.run_id == RUN_ID,
                    NavSnapshotRow.source_cycle_id
                    == next_bootstrap.cycle_id,
                )
            )
        )
    assert {row.arm_id for row in next_open_navs} == {
        arm_id.value
        for arm_id in Q1ArmId
    }
    assert all(
        row.payload_json["session_open_baseline"] is True
        and row.payload_json["calendar_session_id"]
        == next_calendar.calendar_session_id
        for row in next_open_navs
    )


def _cycle(
    *,
    cycle_id: str,
    kind: str,
    scheduled_at: datetime,
) -> PaperCycleRow:
    return PaperCycleRow(
        cycle_id=cycle_id,
        run_id=RUN_ID,
        cycle_kind=kind,
        scheduled_at=scheduled_at,
        data_available_cutoff=scheduled_at,
        status="RUNNING",
        idempotency_key=cycle_id,
        lease_owner="q1-arm-worker",
        lease_expires_at=scheduled_at + timedelta(hours=1),
        attempt_count=1,
        input_manifest_hash=None,
        output_manifest_hash=None,
        started_at=scheduled_at,
        completed_at=None,
        last_error_code=None,
        last_error_detail=None,
        created_at=scheduled_at,
        updated_at=scheduled_at,
    )


def _quote(
    symbol: str,
    index: int,
    *,
    event_time: datetime | None = None,
    quote_prefix: str = "q1-arm-quote",
) -> MarketQuote:
    event_time = event_time or OPEN_AT + timedelta(seconds=1)
    price = Decimal(90 + index)
    payload: dict[str, JsonValue] = {
        "symbol": symbol,
        "event_time": event_time.isoformat(),
        "price": str(price),
    }
    return MarketQuote(
        quote_id=f"{quote_prefix}-{symbol}",
        provider=PROVIDER,
        feed=FEED,
        symbol=symbol,
        event_time=event_time,
        provider_timestamp=event_time.isoformat(),
        available_at=event_time,
        ingested_at=event_time,
        source_kind=MarketDataSourceKind.STREAM_QUOTE,
        bid_exchange="V",
        bid_price=price - Decimal("0.01"),
        bid_size_round_lots=10,
        ask_exchange="V",
        ask_price=price + Decimal("0.01"),
        ask_size_round_lots=10,
        conditions=[],
        tape="C",
        payload_hash=canonical_hash(payload),
        raw_object_uri=None,
        payload=payload,
    )
