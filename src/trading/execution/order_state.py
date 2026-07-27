from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading.domain.enums import OrderSide
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.q1 import (
    OrderEvent,
    OrderEventType,
    is_terminal_order_event,
)
from trading.domain.time import require_aware_utc


class OrderStateError(ValueError):
    """Raised when immutable order events do not form a valid state machine."""


class Q1OrderClass(StrEnum):
    NORMAL = "NORMAL"
    EMERGENCY_REDUCTION = "EMERGENCY_REDUCTION"
    LLM_REDUCTION = "LLM_REDUCTION"
    LIVE_MIRROR_TRANSITION = "LIVE_MIRROR_TRANSITION"


@dataclass(frozen=True, slots=True)
class OrderDescriptor:
    order_intent_id: str
    arm_id: str
    portfolio_decision_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    order_class: Q1OrderClass
    created_at: datetime
    valid_until: datetime

    def __post_init__(self) -> None:
        require_aware_utc(self.created_at)
        require_aware_utc(self.valid_until)
        if not all(
            (
                self.order_intent_id,
                self.arm_id,
                self.portfolio_decision_id,
                self.symbol,
            )
        ):
            raise OrderStateError("Order descriptor identifiers are required")
        if self.quantity <= 0:
            raise OrderStateError("Order quantity must be positive")
        if self.valid_until <= self.created_at:
            raise OrderStateError("Order valid_until must follow created_at")


@dataclass(frozen=True, slots=True)
class OrderEventProvenance:
    config_manifest_hash: str
    code_version: str
    model_version: str
    source_manifest_hash: str
    worker_fence_token: str
    cycle_attempt_count: int

    def __post_init__(self) -> None:
        if not all(
            (
                self.config_manifest_hash,
                self.code_version,
                self.model_version,
                self.source_manifest_hash,
                self.worker_fence_token,
            )
        ):
            raise OrderStateError("Complete order-event provenance is required")
        if self.cycle_attempt_count <= 0:
            raise OrderStateError("Cycle attempt count must be positive")


@dataclass(frozen=True, slots=True)
class OrderAggregate:
    order: OrderDescriptor
    status: OrderEventType
    remaining_quantity: Decimal
    cumulative_filled_quantity: Decimal
    cumulative_commission_usd: Decimal
    latest_event_sequence: int
    latest_event_id: str

    @property
    def is_terminal(self) -> bool:
        return is_terminal_order_event(self.status)

    @property
    def is_pending(self) -> bool:
        return not self.is_terminal and self.remaining_quantity > 0


def reduce_order_events(
    order: OrderDescriptor,
    events: Iterable[OrderEvent],
    *,
    as_of: datetime | None = None,
) -> OrderAggregate:
    """Derive one order's state from its complete append-only event stream."""
    if as_of is not None:
        require_aware_utc(as_of)
    materialized = tuple(
        event
        for event in events
        if event.order_intent_id == order.order_intent_id
        and (as_of is None or event.available_at <= as_of)
    )
    if not materialized:
        raise OrderStateError(f"Order {order.order_intent_id} has no CREATED event")
    _validate_event_identity(materialized)
    ordered = tuple(sorted(materialized, key=lambda event: event.event_sequence))
    if ordered[0].event_type is not OrderEventType.CREATED:
        raise OrderStateError("First order event must be CREATED")
    if ordered[0].event_sequence != 1:
        raise OrderStateError("Order event sequence must start at one")
    if any(
        event.event_sequence != expected
        for expected, event in enumerate(ordered, start=1)
    ):
        raise OrderStateError("Order event sequence must be contiguous")

    remaining = order.quantity
    cumulative_fill = Decimal("0")
    cumulative_commission = Decimal("0")
    previous_status: OrderEventType | None = None
    for event in ordered:
        if event.occurred_at < order.created_at:
            raise OrderStateError("Order event cannot predate its intent")
        if previous_status is not None and is_terminal_order_event(previous_status):
            raise OrderStateError("No order event may follow a terminal event")
        if event.event_type is OrderEventType.CREATED:
            if previous_status is not None:
                raise OrderStateError("CREATED may occur only once")
            if event.quantity_delta != 0 or event.commission_delta_usd != 0:
                raise OrderStateError("CREATED cannot carry fill or commission deltas")
        elif event.event_type in {
            OrderEventType.PARTIALLY_FILLED,
            OrderEventType.FILLED,
        }:
            if event.quantity_delta <= 0:
                raise OrderStateError("Fill event quantity_delta must be positive")
            if event.quantity_delta > remaining:
                raise OrderStateError("Fill event exceeds remaining quantity")
            cumulative_fill += event.quantity_delta
            remaining -= event.quantity_delta
            cumulative_commission += event.commission_delta_usd
            if (
                event.event_type is OrderEventType.PARTIALLY_FILLED
                and remaining <= 0
            ):
                raise OrderStateError("PARTIALLY_FILLED must leave positive quantity")
            if event.event_type is OrderEventType.FILLED and remaining != 0:
                raise OrderStateError("FILLED must consume exact remaining quantity")
        else:
            if event.quantity_delta != 0 or event.commission_delta_usd != 0:
                raise OrderStateError("Non-fill order events cannot carry fill deltas")
        if event.remaining_quantity != remaining:
            raise OrderStateError("Order event remaining_quantity snapshot is inconsistent")
        if event.cumulative_filled_quantity != cumulative_fill:
            raise OrderStateError("Order cumulative fill snapshot is inconsistent")
        if event.cumulative_commission_usd != cumulative_commission:
            raise OrderStateError("Order cumulative commission snapshot is inconsistent")
        previous_status = event.event_type

    latest = ordered[-1]
    return OrderAggregate(
        order=order,
        status=latest.event_type,
        remaining_quantity=remaining,
        cumulative_filled_quantity=cumulative_fill,
        cumulative_commission_usd=cumulative_commission,
        latest_event_sequence=latest.event_sequence,
        latest_event_id=latest.event_id,
    )


def pending_orders(
    orders: Iterable[OrderDescriptor],
    events: Iterable[OrderEvent],
    *,
    as_of: datetime | None = None,
) -> tuple[OrderAggregate, ...]:
    """Return pending orders without consulting a portfolio-decision pointer."""
    materialized_events = tuple(events)
    pending = [
        aggregate
        for order in orders
        if (
            aggregate := reduce_order_events(
                order,
                materialized_events,
                as_of=as_of,
            )
        ).is_pending
    ]
    return tuple(
        sorted(
            pending,
            key=lambda item: (
                item.order.created_at,
                item.order.order_intent_id,
            ),
        )
    )


def append_order_event(
    *,
    order: OrderDescriptor,
    existing_events: Iterable[OrderEvent],
    event_type: OrderEventType,
    occurred_at: datetime,
    available_at: datetime,
    provenance: OrderEventProvenance,
    quantity_delta: Decimal = Decimal("0"),
    commission_delta_usd: Decimal = Decimal("0"),
    reason: str | None = None,
    source_id: str | None = None,
    quote_id: str | None = None,
    source_cycle_id: str | None = None,
) -> OrderEvent:
    """Construct and validate the next immutable event for one order."""
    require_aware_utc(occurred_at)
    require_aware_utc(available_at)
    if available_at < occurred_at:
        raise OrderStateError("Order event available_at cannot precede occurred_at")
    relevant = tuple(
        event
        for event in existing_events
        if event.order_intent_id == order.order_intent_id
    )
    if event_type is OrderEventType.CREATED:
        if relevant:
            raise OrderStateError("CREATED event already exists")
        sequence = 1
        remaining = order.quantity
        cumulative_fill = Decimal("0")
        cumulative_commission = Decimal("0")
        if quantity_delta != 0 or commission_delta_usd != 0:
            raise OrderStateError("CREATED cannot carry fill deltas")
    else:
        aggregate = reduce_order_events(order, relevant)
        if aggregate.is_terminal:
            raise OrderStateError("Cannot append after terminal order event")
        sequence = aggregate.latest_event_sequence + 1
        remaining = aggregate.remaining_quantity
        cumulative_fill = aggregate.cumulative_filled_quantity
        cumulative_commission = aggregate.cumulative_commission_usd
        if event_type in {
            OrderEventType.PARTIALLY_FILLED,
            OrderEventType.FILLED,
        }:
            if quantity_delta <= 0 or quantity_delta > remaining:
                raise OrderStateError("Fill delta must be within remaining quantity")
            remaining -= quantity_delta
            cumulative_fill += quantity_delta
            cumulative_commission += commission_delta_usd
            if (
                event_type is OrderEventType.PARTIALLY_FILLED
                and remaining <= 0
            ):
                raise OrderStateError("PARTIALLY_FILLED must leave a residual")
            if event_type is OrderEventType.FILLED and remaining != 0:
                raise OrderStateError("FILLED must consume the exact residual")
        elif quantity_delta != 0 or commission_delta_usd != 0:
            raise OrderStateError("Non-fill event cannot carry fill deltas")
    identity = {
        "order_intent_id": order.order_intent_id,
        "event_type": event_type,
        "event_sequence": sequence,
        "quantity_delta": quantity_delta,
        "commission_delta_usd": commission_delta_usd,
        "remaining_quantity": remaining,
        "occurred_at": occurred_at,
        "source_id": source_id,
        "quote_id": quote_id,
        "source_cycle_id": source_cycle_id,
    }
    event_id = stable_id("q1-order-event", identity)
    event_hash = canonical_hash(
        {
            **identity,
            "cumulative_filled_quantity": cumulative_fill,
            "cumulative_commission_usd": cumulative_commission,
            "reason": reason,
            "config_manifest_hash": provenance.config_manifest_hash,
            "code_version": provenance.code_version,
            "model_version": provenance.model_version,
            "source_manifest_hash": provenance.source_manifest_hash,
        }
    )
    event = OrderEvent(
        event_id=event_id,
        order_intent_id=order.order_intent_id,
        event_type=event_type,
        event_sequence=sequence,
        quantity_delta=quantity_delta,
        commission_delta_usd=commission_delta_usd,
        remaining_quantity=remaining,
        cumulative_filled_quantity=cumulative_fill,
        cumulative_commission_usd=cumulative_commission,
        occurred_at=occurred_at,
        available_at=available_at,
        idempotency_key=stable_id("q1-order-event-idem", event_id),
        reason=reason,
        source_id=source_id,
        quote_id=quote_id,
        source_cycle_id=source_cycle_id,
        worker_fence_token=provenance.worker_fence_token,
        cycle_attempt_count=provenance.cycle_attempt_count,
        event_hash=event_hash,
        config_manifest_hash=provenance.config_manifest_hash,
        code_version=provenance.code_version,
        model_version=provenance.model_version,
        source_manifest_hash=provenance.source_manifest_hash,
    )
    reduce_order_events(order, (*relevant, event))
    return event


def soft_stop_buy_cancellations(
    *,
    orders: Iterable[OrderDescriptor],
    events: Iterable[OrderEvent],
    occurred_at: datetime,
    available_at: datetime,
    provenance: OrderEventProvenance,
    source_cycle_id: str,
) -> tuple[OrderEvent, ...]:
    """Cancel pending BUYs only; valid pending SELLs remain untouched."""
    materialized = tuple(events)
    result: list[OrderEvent] = []
    for aggregate in pending_orders(orders, materialized):
        if aggregate.order.side is not OrderSide.BUY:
            continue
        event = append_order_event(
            order=aggregate.order,
            existing_events=(*materialized, *result),
            event_type=OrderEventType.CANCELED_BY_RISK,
            occurred_at=occurred_at,
            available_at=available_at,
            provenance=provenance,
            reason="Q1_SOFT_STOP_BLOCK_NEW_BUYS",
            source_cycle_id=source_cycle_id,
        )
        result.append(event)
    return tuple(result)


def supersede_normal_orders(
    *,
    orders: Iterable[OrderDescriptor],
    events: Iterable[OrderEvent],
    replacement_orders: Iterable[OrderDescriptor],
    occurred_at: datetime,
    available_at: datetime,
    provenance: OrderEventProvenance,
    source_cycle_id: str,
) -> tuple[OrderEvent, ...]:
    """Supersede only pending normal orders explicitly replaced by a new target.

    A strategic decision with no normal replacement orders is inert. Pending
    BUYs belong to the prior strategic target and may be superseded when a new
    target creates at least one normal order for the same arm. Pending SELLs
    remain risk-reducing and are superseded only when same-symbol replacement
    SELL quantity is at least their remaining quantity.
    """
    replacements = tuple(
        order
        for order in replacement_orders
        if order.order_class is Q1OrderClass.NORMAL
    )
    if not replacements:
        return ()
    replacement_arms = {order.arm_id for order in replacements}
    replacement_sell_quantity: defaultdict[tuple[str, str], Decimal] = defaultdict(
        Decimal
    )
    for replacement in replacements:
        if replacement.side is OrderSide.SELL:
            replacement_sell_quantity[
                (replacement.arm_id, replacement.symbol)
            ] += replacement.quantity

    materialized = tuple(events)
    result: list[OrderEvent] = []
    for aggregate in pending_orders(orders, materialized):
        if aggregate.order.order_class is not Q1OrderClass.NORMAL:
            continue
        if aggregate.order.arm_id not in replacement_arms:
            continue
        if (
            aggregate.order.side is OrderSide.SELL
            and replacement_sell_quantity[
                (aggregate.order.arm_id, aggregate.order.symbol)
            ]
            < aggregate.remaining_quantity
        ):
            continue
        event = append_order_event(
            order=aggregate.order,
            existing_events=(*materialized, *result),
            event_type=OrderEventType.SUPERSEDED,
            occurred_at=occurred_at,
            available_at=available_at,
            provenance=provenance,
            reason="REPLACED_BY_NEW_STRATEGIC_TARGET",
            source_cycle_id=source_cycle_id,
        )
        result.append(event)
    return tuple(result)


def expire_orders(
    *,
    orders: Iterable[OrderDescriptor],
    events: Iterable[OrderEvent],
    as_of: datetime,
    provenance: OrderEventProvenance,
    source_cycle_id: str,
    expire_at_boundary: bool = False,
) -> tuple[OrderEvent, ...]:
    """Expire after valid_until, or at a non-executable market-close boundary."""
    require_aware_utc(as_of)
    materialized = tuple(events)
    result: list[OrderEvent] = []
    for aggregate in pending_orders(orders, materialized, as_of=as_of):
        if (
            not expire_at_boundary
            and aggregate.order.valid_until >= as_of
        ):
            continue
        result.append(
            append_order_event(
                order=aggregate.order,
                existing_events=(*materialized, *result),
                event_type=OrderEventType.EXPIRED,
                occurred_at=as_of,
                available_at=as_of,
                provenance=provenance,
                reason="ORDER_VALIDITY_WINDOW_ENDED",
                source_cycle_id=source_cycle_id,
            )
        )
    return tuple(result)


def validate_order_event_book(
    orders: Iterable[OrderDescriptor],
    events: Iterable[OrderEvent],
) -> tuple[OrderAggregate, ...]:
    """Validate all event streams and reject orphaned events."""
    materialized_orders = tuple(orders)
    materialized_events = tuple(events)
    order_ids = {order.order_intent_id for order in materialized_orders}
    orphan_ids = sorted(
        {
            event.order_intent_id
            for event in materialized_events
            if event.order_intent_id not in order_ids
        }
    )
    if orphan_ids:
        raise OrderStateError(f"Orphaned order events: {orphan_ids}")
    grouped: defaultdict[str, list[OrderEvent]] = defaultdict(list)
    for event in materialized_events:
        grouped[event.order_intent_id].append(event)
    return tuple(
        reduce_order_events(order, grouped[order.order_intent_id])
        for order in sorted(
            materialized_orders,
            key=lambda item: item.order_intent_id,
        )
    )


def _validate_event_identity(events: tuple[OrderEvent, ...]) -> None:
    event_ids = [event.event_id for event in events]
    idempotency_keys = [event.idempotency_key for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise OrderStateError("Duplicate order event ID")
    if len(idempotency_keys) != len(set(idempotency_keys)):
        raise OrderStateError("Duplicate order event idempotency key")
