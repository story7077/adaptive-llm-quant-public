from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.enums import OrderSide
from trading.domain.hashing import canonical_hash
from trading.domain.q1 import (
    CashSettlementEvent,
    CashSettlementEventType,
    OrderEvent,
    OrderEventType,
)
from trading.domain.q1_runtime import Q1Fill
from trading.domain.time import require_aware_utc
from trading.persistence.models import (
    ArmStateSnapshotRow,
    CashSettlementEventRow,
    FillRow,
    OrderEventRow,
)
from trading.runtime.q1_state import Q1ArmState

Q1_ALGORITHM_VERSION = "q1_math_core_v1"
_FILL_EVENT_TYPES = frozenset(
    {
        OrderEventType.PARTIALLY_FILLED,
        OrderEventType.FILLED,
    }
)


class ReconciliationCondition(StrEnum):
    OK = "OK"
    LEDGER_UNAVAILABLE = "LEDGER_UNAVAILABLE"
    POSITION_OR_CASH_MISMATCH = "POSITION_OR_CASH_MISMATCH"
    NEGATIVE_BALANCE = "NEGATIVE_BALANCE"
    UNKNOWN_FILL = "UNKNOWN_FILL"


@dataclass(frozen=True, slots=True)
class Q1ReconciliationResult:
    conditions: tuple[ReconciliationCondition, ...]
    expected_positions: dict[str, Decimal]
    expected_settled_cash_usd: Decimal
    expected_unsettled_receivables: dict[str, Decimal]
    checked_fill_ids: tuple[str, ...]
    checked_cash_event_ids: tuple[str, ...]
    result_hash: str

    @property
    def primary_condition(self) -> ReconciliationCondition:
        return self.conditions[0]

    @property
    def ok(self) -> bool:
        return self.conditions == (ReconciliationCondition.OK,)

    def is_critical(self, configured_conditions: frozenset[str]) -> bool:
        return any(
            condition.value in configured_conditions
            for condition in self.conditions
        )


class Q1ReconciliationService:
    """Rebuild an arm from immutable opening, fill, and cash ledgers."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
    ) -> None:
        self._session_factory = session_factory

    def check(
        self,
        *,
        run_id: str,
        arm_id: str,
        state: Q1ArmState,
        as_of: datetime,
    ) -> Q1ReconciliationResult:
        instant = require_aware_utc(as_of)
        with self._session_factory() as session:
            opening_row = session.scalar(
                select(ArmStateSnapshotRow)
                .where(
                    ArmStateSnapshotRow.run_id == run_id,
                    ArmStateSnapshotRow.arm_id == arm_id,
                    ArmStateSnapshotRow.created_at <= instant,
                )
                .order_by(
                    ArmStateSnapshotRow.sequence,
                    ArmStateSnapshotRow.created_at,
                    ArmStateSnapshotRow.arm_state_snapshot_id,
                )
                .limit(1)
            )
            fill_rows = tuple(
                session.scalars(
                    select(FillRow)
                    .where(
                        FillRow.run_id == run_id,
                        FillRow.arm_id == arm_id,
                        FillRow.algorithm_version == Q1_ALGORITHM_VERSION,
                        FillRow.effective_at <= instant,
                    )
                    .order_by(FillRow.effective_at, FillRow.fill_id)
                )
            )
            cash_rows = tuple(
                session.scalars(
                    select(CashSettlementEventRow)
                    .where(
                        CashSettlementEventRow.run_id == run_id,
                        CashSettlementEventRow.arm_id == arm_id,
                        CashSettlementEventRow.algorithm_version
                        == Q1_ALGORITHM_VERSION,
                        CashSettlementEventRow.effective_at <= instant,
                        CashSettlementEventRow.created_at <= instant,
                    )
                    .order_by(
                        CashSettlementEventRow.effective_at,
                        CashSettlementEventRow.cash_settlement_event_id,
                    )
                )
            )
            order_event_rows = tuple(
                session.scalars(
                    select(OrderEventRow)
                    .where(
                        OrderEventRow.run_id == run_id,
                        OrderEventRow.arm_id == arm_id,
                        OrderEventRow.algorithm_version
                        == Q1_ALGORITHM_VERSION,
                        OrderEventRow.available_at <= instant,
                    )
                    .order_by(
                        OrderEventRow.available_at,
                        OrderEventRow.order_event_id,
                    )
                )
            )

        conditions: set[ReconciliationCondition] = set()
        if opening_row is None:
            conditions.add(ReconciliationCondition.LEDGER_UNAVAILABLE)
            opening_state = state
        else:
            try:
                opening_state = Q1ArmState.from_payload(
                    opening_row.payload_json
                )
            except Exception:
                conditions.add(ReconciliationCondition.LEDGER_UNAVAILABLE)
                opening_state = state
        if opening_state.sequence != 0:
            conditions.add(ReconciliationCondition.LEDGER_UNAVAILABLE)

        fills: list[Q1Fill] = []
        for row in fill_rows:
            try:
                fill = Q1Fill.model_validate(row.payload_json)
            except Exception:
                conditions.add(ReconciliationCondition.UNKNOWN_FILL)
                continue
            if (
                fill.fill_id != row.fill_id
                or fill.fill_hash != row.fill_hash
                or fill.run_id != run_id
                or fill.arm_id.value != arm_id
                or fill.created_at > instant
            ):
                conditions.add(ReconciliationCondition.UNKNOWN_FILL)
                continue
            fills.append(fill)

        fill_events: list[OrderEvent] = []
        for row in order_event_rows:
            if row.event_type not in {
                item.value
                for item in _FILL_EVENT_TYPES
            }:
                continue
            try:
                event = OrderEvent.model_validate(row.payload_json)
            except Exception:
                conditions.add(ReconciliationCondition.UNKNOWN_FILL)
                continue
            fill_events.append(event)
        fill_ids = {fill.fill_id for fill in fills}
        for fill in fills:
            matches = [
                event
                for event in fill_events
                if (
                    event.source_id == fill.fill_id
                    and event.order_intent_id == fill.order_intent_id
                    and event.quantity_delta == fill.quantity
                    and event.commission_delta_usd == fill.commission_usd
                )
            ]
            if len(matches) != 1:
                conditions.add(ReconciliationCondition.UNKNOWN_FILL)
        if any(
            event.source_id is None or event.source_id not in fill_ids
            for event in fill_events
        ):
            conditions.add(ReconciliationCondition.UNKNOWN_FILL)

        expected_positions = dict(opening_state.positions)
        for fill in fills:
            signed_quantity = (
                fill.quantity
                if fill.side is OrderSide.BUY
                else -fill.quantity
            )
            expected_positions[fill.symbol] = (
                expected_positions.get(fill.symbol, Decimal("0"))
                + signed_quantity
            )
        expected_positions = _positive_positions(expected_positions)

        cash_events: list[CashSettlementEvent] = []
        for row in cash_rows:
            try:
                event = CashSettlementEvent.model_validate(
                    row.payload_json
                )
            except Exception:
                conditions.add(
                    ReconciliationCondition.POSITION_OR_CASH_MISMATCH
                )
                continue
            if (
                event.cash_settlement_event_id
                != row.cash_settlement_event_id
                or event.event_hash != row.event_hash
            ):
                conditions.add(
                    ReconciliationCondition.POSITION_OR_CASH_MISMATCH
                )
                continue
            cash_events.append(event)

        opening_cash_events = [
            event
            for event in cash_events
            if event.event_type
            is CashSettlementEventType.OPENING_SETTLED_CASH
        ]
        if len(opening_cash_events) != 1:
            conditions.add(ReconciliationCondition.LEDGER_UNAVAILABLE)
        expected_settled = sum(
            (event.settled_cash_delta_usd for event in cash_events),
            Decimal("0"),
        )
        expected_receivables: dict[str, Decimal] = {}
        cash_events_by_fill: dict[str, list[CashSettlementEvent]] = {}
        for event in cash_events:
            if event.source_fill_id is not None:
                cash_events_by_fill.setdefault(
                    event.source_fill_id,
                    [],
                ).append(event)
            if event.receivable_id is not None:
                expected_receivables[event.receivable_id] = (
                    expected_receivables.get(
                        event.receivable_id,
                        Decimal("0"),
                    )
                    + event.unsettled_receivable_delta_usd
                )
        expected_receivables = {
            receivable_id: amount
            for receivable_id, amount in expected_receivables.items()
            if amount != 0
        }
        if set(cash_events_by_fill) != fill_ids:
            conditions.add(ReconciliationCondition.UNKNOWN_FILL)
        for fill in fills:
            event_types = tuple(
                event.event_type
                for event in cash_events_by_fill.get(fill.fill_id, ())
            )
            if fill.side is OrderSide.BUY:
                valid_cash_events = event_types == (
                    CashSettlementEventType.BUY_SETTLED_CASH_DEBIT,
                )
            else:
                valid_cash_events = (
                    event_types
                    in {
                        (
                            CashSettlementEventType.SELL_RECEIVABLE_CREATED,
                        ),
                        (
                            CashSettlementEventType.SELL_RECEIVABLE_CREATED,
                            CashSettlementEventType.RECEIVABLE_SETTLED,
                        ),
                    }
                )
            if not valid_cash_events:
                conditions.add(ReconciliationCondition.UNKNOWN_FILL)

        settlement_count = sum(
            event.event_type
            is CashSettlementEventType.RECEIVABLE_SETTLED
            for event in cash_events
        )
        if state.sequence != len(fills) + settlement_count:
            conditions.add(
                ReconciliationCondition.POSITION_OR_CASH_MISMATCH
            )

        actual_receivables = {
            item.receivable_id: item.amount_usd
            for item in state.unsettled_receivables
        }
        if (
            expected_positions != _positive_positions(state.positions)
            or expected_settled != state.settled_cash_usd
            or expected_receivables != actual_receivables
        ):
            conditions.add(
                ReconciliationCondition.POSITION_OR_CASH_MISMATCH
            )
        if (
            state.settled_cash_usd < 0
            or expected_settled < 0
            or any(quantity < 0 for quantity in expected_positions.values())
            or any(amount < 0 for amount in expected_receivables.values())
        ):
            conditions.add(ReconciliationCondition.NEGATIVE_BALANCE)

        ordered_conditions = tuple(
            condition
            for condition in ReconciliationCondition
            if condition is not ReconciliationCondition.OK
            and condition in conditions
        )
        if not ordered_conditions:
            ordered_conditions = (ReconciliationCondition.OK,)
        result_content = {
            "run_id": run_id,
            "arm_id": arm_id,
            "as_of": instant,
            "state_sequence": state.sequence,
            "conditions": ordered_conditions,
            "expected_positions": expected_positions,
            "expected_settled_cash_usd": expected_settled,
            "expected_unsettled_receivables": expected_receivables,
            "checked_fill_ids": tuple(fill.fill_id for fill in fills),
            "checked_cash_event_ids": tuple(
                event.cash_settlement_event_id
                for event in cash_events
            ),
        }
        return Q1ReconciliationResult(
            conditions=ordered_conditions,
            expected_positions=expected_positions,
            expected_settled_cash_usd=expected_settled,
            expected_unsettled_receivables=expected_receivables,
            checked_fill_ids=tuple(fill.fill_id for fill in fills),
            checked_cash_event_ids=tuple(
                event.cash_settlement_event_id
                for event in cash_events
            ),
            result_hash=canonical_hash(result_content),
        )


def _positive_positions(
    positions: dict[str, Decimal],
) -> dict[str, Decimal]:
    return {
        symbol: quantity
        for symbol, quantity in sorted(positions.items())
        if quantity != 0
    }
