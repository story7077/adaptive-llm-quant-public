from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading.domain.contracts import model_payload
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.q1 import OrderEvent, Q1StrategyDecision
from trading.domain.q1_runtime import Q1Fill, Q1OrderIntent
from trading.domain.time import require_aware_utc
from trading.execution.order_state import OrderDescriptor, Q1OrderClass
from trading.persistence.models import (
    ArmStateSnapshotRow,
    FillRow,
    NavSnapshotRow,
    OrderEventRow,
    OrderIntentRow,
    PaperCycleRow,
    PortfolioDecisionRow,
    RiskDecisionRow,
)
from trading.persistence.q1 import Q1StrategyDecisionRepository
from trading.runtime.q1_state import Q1ArmState


class Q1RuntimePersistenceError(RuntimeError):
    pass


class Q1StaleWorkerError(Q1RuntimePersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class Q1OrderBook:
    intents: tuple[Q1OrderIntent, ...]
    descriptors: tuple[OrderDescriptor, ...]
    events: tuple[OrderEvent, ...]


def require_cycle_fence(
    session: Session,
    *,
    cycle_id: str,
    lease_owner: str,
    attempt_count: int,
    fallback_now: datetime,
) -> PaperCycleRow:
    statement = select(PaperCycleRow).where(PaperCycleRow.cycle_id == cycle_id)
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    cycle = session.scalar(statement)
    if cycle is None:
        raise Q1StaleWorkerError(f"Unknown Q1 cycle {cycle_id!r}")
    database_now = _database_now(session, fallback_now)
    if (
        cycle.status != "RUNNING"
        or cycle.lease_owner != lease_owner
        or cycle.attempt_count != attempt_count
        or cycle.lease_expires_at is None
        or _aware(cycle.lease_expires_at) <= database_now
    ):
        raise Q1StaleWorkerError("Q1 cycle lease was lost or reclaimed")
    return cycle


def complete_fenced_cycle(
    cycle: PaperCycleRow,
    *,
    cutoff: datetime,
    input_manifest: object,
    output_manifest: object,
    completed_at: datetime,
) -> None:
    instant = require_aware_utc(completed_at)
    cycle.data_available_cutoff = require_aware_utc(cutoff)
    cycle.input_manifest_hash = canonical_hash(input_manifest)
    cycle.output_manifest_hash = canonical_hash(output_manifest)
    cycle.status = "COMPLETED"
    cycle.completed_at = instant
    cycle.lease_owner = None
    cycle.lease_expires_at = None
    cycle.last_error_code = None
    cycle.last_error_detail = None
    cycle.updated_at = instant


def latest_arm_state(
    session: Session,
    *,
    run_id: str,
    arm_id: str,
    lock: bool = False,
) -> Q1ArmState | None:
    statement = (
        select(ArmStateSnapshotRow)
        .where(
            ArmStateSnapshotRow.run_id == run_id,
            ArmStateSnapshotRow.arm_id == arm_id,
        )
        .order_by(
            ArmStateSnapshotRow.sequence.desc(),
            ArmStateSnapshotRow.arm_state_snapshot_id.desc(),
        )
        .limit(1)
    )
    if lock and session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    row = session.scalar(statement)
    if row is None:
        return None
    return Q1ArmState.from_payload(row.payload_json)


def append_arm_state(
    session: Session,
    *,
    run_id: str,
    state: Q1ArmState,
    source_cycle_id: str,
    created_at: datetime,
    expected_previous_sequence: int | None,
) -> ArmStateSnapshotRow:
    instant = require_aware_utc(created_at)
    current = latest_arm_state(
        session,
        run_id=run_id,
        arm_id=state.arm_id,
        lock=True,
    )
    actual_previous = None if current is None else current.sequence
    if actual_previous != expected_previous_sequence:
        raise Q1RuntimePersistenceError(
            f"Arm {state.arm_id} state changed during Q1 preparation"
        )
    expected_sequence = 0 if current is None else current.sequence + 1
    if state.sequence != expected_sequence:
        raise Q1RuntimePersistenceError(
            f"Arm {state.arm_id} sequence must advance to {expected_sequence}"
        )
    payload = state.as_payload()
    state_hash = canonical_hash(payload)
    row_id = stable_id(
        "q1-arm-state",
        run_id,
        state.arm_id,
        state.sequence,
        source_cycle_id,
        state_hash,
    )
    existing = session.get(ArmStateSnapshotRow, row_id)
    if existing is not None:
        if existing.state_hash != state_hash:
            raise Q1RuntimePersistenceError("Q1 arm-state identity conflict")
        return existing
    row = ArmStateSnapshotRow(
        arm_state_snapshot_id=row_id,
        run_id=run_id,
        arm_id=state.arm_id,
        sequence=state.sequence,
        source_cycle_id=source_cycle_id,
        state_hash=state_hash,
        payload_json=payload,
        created_at=instant,
    )
    session.add(row)
    return row


def append_strategy_decision(
    session: Session,
    *,
    decision: Q1StrategyDecision,
) -> PortfolioDecisionRow:
    return Q1StrategyDecisionRepository(session).append(decision)


def append_order_intent(
    session: Session,
    intent: Q1OrderIntent,
) -> OrderIntentRow:
    existing = session.scalar(
        select(OrderIntentRow).where(
            OrderIntentRow.idempotency_key == intent.idempotency_key
        )
    )
    if existing is not None:
        if existing.intent_hash != intent.intent_hash:
            raise Q1RuntimePersistenceError("Q1 intent idempotency conflict")
        return existing
    row = OrderIntentRow(
        order_intent_id=intent.order_intent_id,
        run_id=intent.run_id,
        arm_id=intent.arm_id.value,
        source_cycle_id=intent.source_cycle_id,
        input_state_sequence=intent.input_state_sequence,
        symbol=intent.symbol,
        side=intent.side.value,
        quantity=intent.quantity,
        created_at=intent.created_at,
        valid_until=intent.valid_until,
        decision_quote_id=intent.decision_quote_id,
        decision_reference_price=intent.decision_reference_price,
        algorithm_version=intent.algorithm_version,
        config_manifest_hash=intent.config_manifest_hash,
        code_version=intent.code_version,
        model_version=intent.model_version,
        source_manifest_hash=intent.source_manifest_hash,
        decision_spread_bps=intent.decision_spread_bps,
        idempotency_key=intent.idempotency_key,
        payload_json=model_payload(intent),
        intent_hash=intent.intent_hash,
    )
    session.add(row)
    return row


def append_risk_approval(
    session: Session,
    *,
    risk_decision_id: str,
    decision: Q1StrategyDecision,
) -> RiskDecisionRow:
    payload = {
        "schema_version": "q1_risk_approval_v1",
        "algorithm_version": decision.algorithm_version,
        "risk_decision_id": risk_decision_id,
        "portfolio_decision_id": decision.portfolio_decision_id,
        "approved": True,
        "approved_target_weights": {
            symbol: str(value)
            for symbol, value in sorted(decision.target_weights.items())
        },
        "deterministic_risk_precedence": True,
        "real_order_routing": False,
        "config_manifest_hash": decision.config_manifest_hash,
        "source_manifest_hash": decision.source_manifest_hash,
    }
    existing = session.get(RiskDecisionRow, risk_decision_id)
    if existing is not None:
        if canonical_hash(existing.payload_json) != canonical_hash(payload):
            raise Q1RuntimePersistenceError("Q1 risk approval identity conflict")
        return existing
    row = RiskDecisionRow(
        risk_decision_id=risk_decision_id,
        portfolio_decision_id=decision.portfolio_decision_id,
        source_cycle_id=decision.source_cycle_id,
        input_state_sequence=decision.input_state_sequence,
        approved=True,
        payload_json=payload,
    )
    session.add(row)
    return row


def append_fill(session: Session, fill: Q1Fill) -> FillRow:
    existing = session.get(FillRow, fill.fill_id)
    if existing is not None:
        if existing.fill_hash != fill.fill_hash:
            raise Q1RuntimePersistenceError("Q1 fill identity conflict")
        return existing
    duplicate = session.scalar(
        select(FillRow).where(
            FillRow.order_intent_id == fill.order_intent_id,
            FillRow.quote_id == fill.quote_id,
            FillRow.execution_scenario_id == fill.execution_scenario_id,
        )
    )
    if duplicate is not None:
        if duplicate.fill_hash != fill.fill_hash:
            raise Q1RuntimePersistenceError(
                "Order/quote/scenario already produced a different fill"
            )
        return duplicate
    row = FillRow(
        fill_id=fill.fill_id,
        order_intent_id=fill.order_intent_id,
        run_id=fill.run_id,
        arm_id=fill.arm_id.value,
        source_cycle_id=fill.source_cycle_id,
        quote_id=fill.quote_id,
        quote_event_time=fill.quote_event_time,
        quote_available_at=fill.quote_available_at,
        symbol=fill.symbol,
        side=fill.side.value,
        quantity=fill.quantity,
        price=fill.price,
        commission_usd=fill.commission_usd,
        execution_scenario_id=fill.execution_scenario_id,
        fill_hash=fill.fill_hash,
        algorithm_version=fill.algorithm_version,
        config_manifest_hash=fill.config_manifest_hash,
        code_version=fill.code_version,
        model_version=fill.model_version,
        source_manifest_hash=fill.source_manifest_hash,
        base_fill_cost_usd=fill.base_fill_cost_usd,
        sensitivity_5bp_cost_usd=fill.sensitivity_5bp_cost_usd,
        sensitivity_10bp_cost_usd=fill.sensitivity_10bp_cost_usd,
        effective_at=fill.effective_at,
        payload_json=model_payload(fill),
    )
    session.add(row)
    return row


def append_nav_snapshot(
    session: Session,
    *,
    run_id: str,
    arm_id: str,
    source_cycle_id: str,
    as_of: datetime,
    nav_usd: Decimal,
    payload: dict[str, Any],
    quote_manifest_hash: str,
    algorithm_version: str,
    config_manifest_hash: str,
    code_version: str,
    model_version: str,
    source_manifest_hash: str,
) -> NavSnapshotRow:
    instant = require_aware_utc(as_of)
    nav_hash = canonical_hash(payload)
    row_id = stable_id(
        "q1-nav",
        run_id,
        arm_id,
        source_cycle_id,
        instant,
        nav_hash,
    )
    existing = session.get(NavSnapshotRow, row_id)
    if existing is not None:
        if existing.source_manifest_hash != source_manifest_hash:
            raise Q1RuntimePersistenceError("Q1 NAV identity conflict")
        return existing
    row = NavSnapshotRow(
        nav_snapshot_id=row_id,
        run_id=run_id,
        arm_id=arm_id,
        source_cycle_id=source_cycle_id,
        quote_manifest_hash=quote_manifest_hash,
        algorithm_version=algorithm_version,
        config_manifest_hash=config_manifest_hash,
        code_version=code_version,
        model_version=model_version,
        source_manifest_hash=source_manifest_hash,
        as_of=instant,
        nav_usd=nav_usd,
        payload_json={**payload, "nav_hash": nav_hash},
    )
    session.add(row)
    return row


def load_q1_order_book(
    session: Session,
    *,
    run_id: str,
    arm_id: str | None = None,
) -> Q1OrderBook:
    intent_statement = select(OrderIntentRow).where(
        OrderIntentRow.run_id == run_id,
        OrderIntentRow.algorithm_version == "q1_math_core_v1",
    )
    event_statement = select(OrderEventRow).where(
        OrderEventRow.run_id == run_id,
        OrderEventRow.algorithm_version == "q1_math_core_v1",
    )
    if arm_id is not None:
        intent_statement = intent_statement.where(OrderIntentRow.arm_id == arm_id)
        event_statement = event_statement.where(OrderEventRow.arm_id == arm_id)
    intent_rows = tuple(
        session.scalars(
            intent_statement.order_by(
                OrderIntentRow.created_at,
                OrderIntentRow.order_intent_id,
            )
        )
    )
    event_rows = tuple(
        session.scalars(
            event_statement.order_by(
                OrderEventRow.order_intent_id,
                OrderEventRow.event_sequence,
            )
        )
    )
    intents = tuple(
        Q1OrderIntent.model_validate(row.payload_json)
        for row in intent_rows
    )
    descriptors = tuple(
        OrderDescriptor(
            order_intent_id=item.order_intent_id,
            arm_id=item.arm_id.value,
            portfolio_decision_id=item.portfolio_decision_id,
            symbol=item.symbol,
            side=item.side,
            quantity=item.quantity,
            order_class=Q1OrderClass(item.order_class),
            created_at=item.created_at,
            valid_until=item.valid_until,
        )
        for item in intents
    )
    events = tuple(
        OrderEvent.model_validate(row.payload_json)
        for row in event_rows
    )
    return Q1OrderBook(
        intents=intents,
        descriptors=descriptors,
        events=events,
    )


def _database_now(session: Session, fallback: datetime) -> datetime:
    instant = require_aware_utc(fallback)
    if session.get_bind().dialect.name != "postgresql":
        return instant
    value = session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime):
        raise Q1RuntimePersistenceError("PostgreSQL database clock is unavailable")
    return _aware(value)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
