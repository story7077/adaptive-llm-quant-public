from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.q1 import Q1ArmId
from trading.persistence.models import PaperCycleRow, RunRow
from trading.persistence.q1 import CashSettlementRepository
from trading.persistence.q1_runtime import append_arm_state
from trading.risk.reconciliation import (
    Q1ReconciliationService,
    ReconciliationCondition,
)
from trading.runtime.q1_config import (
    critical_reconciliation_conditions,
    settlement_policy,
)
from trading.runtime.q1_paper import Q1PaperRuntimeService
from trading.runtime.q1_scheduler import VersionedMarketSession
from trading.runtime.q1_state import Q1ArmState
from trading.settings import load_q1_config_bundle
from trading.settlement.service import (
    SettlementProvenance,
    record_opening_settled_cash,
)

NOW = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
HASH = "a" * 64


def test_q1_reconciliation_rebuilds_opening_cash_and_is_deterministic(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    config = load_q1_config_bundle(repository_root / "config")
    runtime = Q1PaperRuntimeService(
        factory,
        config=config,
        workspace_root=repository_root,
    )
    calendar = VersionedMarketSession(
        calendar_session_id="reconciliation-calendar",
        calendar_version="alpaca_market_calendar_v1",
        session_date=date(2026, 7, 27),
        open_at=NOW - timedelta(minutes=30),
        close_at=NOW + timedelta(hours=6),
        source_payload_hash=HASH,
        source_available_at=NOW - timedelta(days=7),
    )
    runtime.register_calendar_session(
        calendar,
        now=NOW - timedelta(days=1),
    )
    cycle = PaperCycleRow(
        cycle_id="reconciliation-cycle",
        run_id="reconciliation-run",
        cycle_kind="Q1_BOOTSTRAP",
        scheduled_at=NOW,
        data_available_cutoff=NOW,
        status="RUNNING",
        idempotency_key="reconciliation-cycle",
        lease_owner="test-worker",
        lease_expires_at=NOW + timedelta(hours=1),
        attempt_count=1,
        input_manifest_hash=None,
        output_manifest_hash=None,
        started_at=NOW,
        completed_at=None,
        last_error_code=None,
        last_error_detail=None,
        created_at=NOW,
        updated_at=NOW,
    )
    state = Q1ArmState(
        arm_id=Q1ArmId.Q1_DET.value,
        initial_nav_usd=Decimal("1000"),
        settled_cash_usd=Decimal("1000"),
        unsettled_receivables=(),
        positions={},
        sequence=0,
        evaluation_anchor_id="reconciliation-anchor",
    )
    provenance = SettlementProvenance(
        run_id=cycle.run_id,
        source_cycle_id=cycle.cycle_id,
        config_manifest_hash=config.manifest_hash,
        code_version="test-code",
        model_version="test-model",
        source_manifest_hash=HASH,
        worker_fence_token="test-worker",
        cycle_attempt_count=1,
    )
    opening_cash = record_opening_settled_cash(
        arm_id=Q1ArmId.Q1_DET,
        amount_usd=Decimal("1000"),
        effective_at=NOW,
        created_at=NOW,
        calendar_session_id=calendar.calendar_session_id,
        policy=settlement_policy(config),
        provenance=provenance,
    )
    with factory.begin() as session:
        run = RunRow(
            run_id=cycle.run_id,
            mode="PAPER",
            experiment_version="q1_math_core_v1",
            config_manifest_hash=config.manifest_hash,
            code_commit="test-code",
            started_at=NOW,
            ended_at=None,
            status="RUNNING",
            result_manifest={"real_order_routing": False},
            result_hash=None,
        )
        session.add(run)
        session.flush()
        session.add(cycle)
        session.flush()
        append_arm_state(
            session,
            run_id=cycle.run_id,
            state=state,
            source_cycle_id=cycle.cycle_id,
            created_at=NOW,
            expected_previous_sequence=None,
        )
        CashSettlementRepository(session).append(opening_cash)

    service = Q1ReconciliationService(factory)
    first = service.check(
        run_id=cycle.run_id,
        arm_id=state.arm_id,
        state=state,
        as_of=NOW,
    )
    second = service.check(
        run_id=cycle.run_id,
        arm_id=state.arm_id,
        state=state,
        as_of=NOW,
    )

    assert first.ok is True
    assert first.conditions == (ReconciliationCondition.OK,)
    assert first.result_hash == second.result_hash

    mismatched = service.check(
        run_id=cycle.run_id,
        arm_id=state.arm_id,
        state=Q1ArmState(
            arm_id=state.arm_id,
            initial_nav_usd=state.initial_nav_usd,
            settled_cash_usd=Decimal("999"),
            unsettled_receivables=(),
            positions={},
            sequence=state.sequence,
            evaluation_anchor_id=state.evaluation_anchor_id,
        ),
        as_of=NOW,
    )
    assert ReconciliationCondition.POSITION_OR_CASH_MISMATCH in (
        mismatched.conditions
    )
    assert mismatched.is_critical(
        critical_reconciliation_conditions(config)
    )
    assert mismatched.result_hash != first.result_hash
