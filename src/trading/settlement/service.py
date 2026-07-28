from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.q1 import (
    CashSettlementEvent,
    CashSettlementEventType,
    Q1ArmId,
)
from trading.domain.time import require_aware_utc


class SettlementError(ValueError):
    """Raised when a cash-settlement transition is invalid."""


@dataclass(frozen=True, slots=True)
class SettlementPolicy:
    version: str
    calendar_version: str
    lag_business_sessions: int

    def __post_init__(self) -> None:
        if not self.version:
            raise SettlementError("Settlement policy version is required")
        if not self.calendar_version:
            raise SettlementError("Settlement calendar version is required")
        if self.lag_business_sessions < 0:
            raise SettlementError("Settlement lag cannot be negative")


@dataclass(frozen=True, slots=True)
class BusinessCalendar:
    """A versioned, already-vetted sequence of actual market sessions."""

    version: str
    sessions: tuple[date, ...]

    def __post_init__(self) -> None:
        if not self.version:
            raise SettlementError("Business-calendar version is required")
        if not self.sessions:
            raise SettlementError("Business calendar must contain sessions")
        if tuple(sorted(set(self.sessions))) != self.sessions:
            raise SettlementError(
                "Business calendar sessions must be unique and strictly increasing"
            )

    def require_session(self, session_date: date) -> None:
        index = bisect_right(self.sessions, session_date)
        if index == 0 or self.sessions[index - 1] != session_date:
            raise SettlementError(f"{session_date.isoformat()} is not a calendar session")

    def add_business_sessions(self, session_date: date, count: int) -> date:
        if count < 0:
            raise SettlementError("Business-session offset cannot be negative")
        self.require_session(session_date)
        start = bisect_right(self.sessions, session_date) - 1
        target = start + count
        if target >= len(self.sessions):
            raise SettlementError("Business calendar does not cover settlement date")
        return self.sessions[target]


@dataclass(frozen=True, slots=True)
class SettlementProvenance:
    run_id: str
    source_cycle_id: str
    config_manifest_hash: str
    code_version: str
    model_version: str
    source_manifest_hash: str
    worker_fence_token: str
    cycle_attempt_count: int

    def __post_init__(self) -> None:
        if not all(
            (
                self.run_id,
                self.source_cycle_id,
                self.config_manifest_hash,
                self.code_version,
                self.model_version,
                self.source_manifest_hash,
                self.worker_fence_token,
            )
        ):
            raise SettlementError("Complete settlement provenance is required")
        if self.cycle_attempt_count <= 0:
            raise SettlementError("Settlement cycle attempt must be positive")


@dataclass(frozen=True, slots=True)
class CashBalances:
    settled_cash_usd: Decimal
    unsettled_receivables: tuple[tuple[str, Decimal, date], ...]

    def __post_init__(self) -> None:
        if self.settled_cash_usd < 0:
            raise SettlementError("Settled cash cannot be negative")
        if any(amount <= 0 for _, amount, _ in self.unsettled_receivables):
            raise SettlementError("Unsettled receivables must be positive")

    @property
    def unsettled_receivables_usd(self) -> Decimal:
        return sum(
            (amount for _, amount, _ in self.unsettled_receivables),
            Decimal("0"),
        )

    @property
    def total_cash_usd(self) -> Decimal:
        return self.settled_cash_usd + self.unsettled_receivables_usd


def record_opening_settled_cash(
    *,
    arm_id: Q1ArmId,
    amount_usd: Decimal,
    effective_at: datetime,
    created_at: datetime,
    calendar_session_id: str,
    policy: SettlementPolicy,
    provenance: SettlementProvenance,
) -> CashSettlementEvent:
    if amount_usd < 0:
        raise SettlementError("Opening settled cash cannot be negative")
    return _event(
        arm_id=arm_id,
        event_type=CashSettlementEventType.OPENING_SETTLED_CASH,
        receivable_id=None,
        source_fill_id=None,
        settled_delta=amount_usd,
        unsettled_delta=Decimal("0"),
        gross_amount=amount_usd,
        commission=Decimal("0"),
        trade_at=None,
        settlement_date=None,
        effective_at=effective_at,
        created_at=created_at,
        calendar_session_id=calendar_session_id,
        policy=policy,
        provenance=provenance,
    )


def record_buy_cash_debit(
    *,
    arm_id: Q1ArmId,
    fill_id: str,
    trade_at: datetime,
    fill_notional_usd: Decimal,
    commission_usd: Decimal,
    created_at: datetime,
    calendar_session_id: str,
    policy: SettlementPolicy,
    provenance: SettlementProvenance,
) -> CashSettlementEvent:
    """Create the immutable settled-cash debit caused by a BUY fill."""
    if fill_notional_usd <= 0 or commission_usd < 0:
        raise SettlementError("BUY fill notional must be positive and commission non-negative")
    return _event(
        arm_id=arm_id,
        event_type=CashSettlementEventType.BUY_SETTLED_CASH_DEBIT,
        receivable_id=None,
        source_fill_id=fill_id,
        settled_delta=-(fill_notional_usd + commission_usd),
        unsettled_delta=Decimal("0"),
        gross_amount=fill_notional_usd,
        commission=commission_usd,
        trade_at=trade_at,
        settlement_date=None,
        effective_at=trade_at,
        created_at=created_at,
        calendar_session_id=calendar_session_id,
        policy=policy,
        provenance=provenance,
    )


def record_sell_receivable(
    *,
    arm_id: Q1ArmId,
    fill_id: str,
    trade_at: datetime,
    trade_session: date,
    fill_notional_usd: Decimal,
    commission_usd: Decimal,
    created_at: datetime,
    calendar_session_id: str,
    policy: SettlementPolicy,
    calendar: BusinessCalendar,
    provenance: SettlementProvenance,
) -> CashSettlementEvent:
    """Create an unsettled receivable net of commission for a SELL fill."""
    _validate_policy_calendar(policy, calendar)
    calendar.require_session(trade_session)
    if fill_notional_usd <= 0 or commission_usd < 0:
        raise SettlementError("SELL fill notional must be positive and commission non-negative")
    net_proceeds = fill_notional_usd - commission_usd
    if net_proceeds <= 0:
        raise SettlementError("SELL net proceeds must be positive")
    settlement_date = calendar.add_business_sessions(
        trade_session,
        policy.lag_business_sessions,
    )
    receivable_id = stable_id(
        "q1-receivable",
        provenance.run_id,
        arm_id,
        fill_id,
        policy.version,
    )
    return _event(
        arm_id=arm_id,
        event_type=CashSettlementEventType.SELL_RECEIVABLE_CREATED,
        receivable_id=receivable_id,
        source_fill_id=fill_id,
        settled_delta=Decimal("0"),
        unsettled_delta=net_proceeds,
        gross_amount=fill_notional_usd,
        commission=commission_usd,
        trade_at=trade_at,
        settlement_date=settlement_date,
        effective_at=trade_at,
        created_at=created_at,
        calendar_session_id=calendar_session_id,
        policy=policy,
        provenance=provenance,
    )


def settle_due_receivables(
    *,
    events: Iterable[CashSettlementEvent],
    through_session: date,
    effective_at: datetime,
    created_at: datetime,
    calendar_session_id: str,
    policy: SettlementPolicy,
    calendar: BusinessCalendar,
    provenance: SettlementProvenance,
) -> tuple[CashSettlementEvent, ...]:
    """Return only missing settlement events; replaying the result is idempotent."""
    _validate_policy_calendar(policy, calendar)
    calendar.require_session(through_session)
    require_aware_utc(effective_at)
    require_aware_utc(created_at)
    materialized = tuple(events)
    _validate_unique_events(materialized)
    created_by_receivable = {
        event.receivable_id: event
        for event in materialized
        if (
            event.event_type is CashSettlementEventType.SELL_RECEIVABLE_CREATED
            and event.receivable_id is not None
        )
    }
    already_settled = {
        event.receivable_id
        for event in materialized
        if event.event_type is CashSettlementEventType.RECEIVABLE_SETTLED
    }
    result: list[CashSettlementEvent] = []
    for receivable_id, source in sorted(created_by_receivable.items()):
        if (
            receivable_id in already_settled
            or source.settlement_date is None
            or source.settlement_date > through_session
        ):
            continue
        result.append(
            _event(
                arm_id=source.arm_id,
                event_type=CashSettlementEventType.RECEIVABLE_SETTLED,
                receivable_id=receivable_id,
                source_fill_id=source.source_fill_id,
                settled_delta=source.unsettled_receivable_delta_usd,
                unsettled_delta=-source.unsettled_receivable_delta_usd,
                gross_amount=source.gross_amount_usd,
                commission=source.commission_usd,
                trade_at=source.trade_at,
                settlement_date=source.settlement_date,
                effective_at=effective_at,
                created_at=created_at,
                calendar_session_id=calendar_session_id,
                policy=policy,
                provenance=provenance,
            )
        )
    return tuple(result)


def apply_settlement_events(
    *,
    events: Iterable[CashSettlementEvent],
    as_of: datetime,
) -> CashBalances:
    """Derive settled and unsettled cash solely from point-in-time immutable events."""
    require_aware_utc(as_of)
    materialized = tuple(events)
    _validate_unique_events(materialized)
    settled = Decimal("0")
    receivables: dict[str, tuple[Decimal, date]] = {}
    ordered = sorted(
        (
            event
            for event in materialized
            if event.effective_at <= as_of and event.created_at <= as_of
        ),
        key=lambda event: (
            event.effective_at,
            _EVENT_ORDER[event.event_type],
            event.cash_settlement_event_id,
        ),
    )
    for event in ordered:
        settled += event.settled_cash_delta_usd
        if event.event_type is CashSettlementEventType.SELL_RECEIVABLE_CREATED:
            if event.receivable_id is None or event.settlement_date is None:
                raise SettlementError("SELL receivable has incomplete identity")
            if event.receivable_id in receivables:
                raise SettlementError("SELL fill created more than one receivable")
            receivables[event.receivable_id] = (
                event.unsettled_receivable_delta_usd,
                event.settlement_date,
            )
        elif event.event_type is CashSettlementEventType.RECEIVABLE_SETTLED:
            if event.receivable_id is None:
                raise SettlementError("Settlement event has no receivable identity")
            receivable = receivables.pop(event.receivable_id, None)
            if receivable is None:
                raise SettlementError("Settlement event has no open receivable")
            if receivable[0] != -event.unsettled_receivable_delta_usd:
                raise SettlementError("Settlement amount differs from receivable")
        if settled < 0:
            raise SettlementError("BUY fill would consume more than settled cash")
    unsettled = tuple(
        (receivable_id, amount, settlement_date)
        for receivable_id, (amount, settlement_date) in sorted(receivables.items())
    )
    return CashBalances(
        settled_cash_usd=settled,
        unsettled_receivables=unsettled,
    )


def _event(
    *,
    arm_id: Q1ArmId,
    event_type: CashSettlementEventType,
    receivable_id: str | None,
    source_fill_id: str | None,
    settled_delta: Decimal,
    unsettled_delta: Decimal,
    gross_amount: Decimal,
    commission: Decimal,
    trade_at: datetime | None,
    settlement_date: date | None,
    effective_at: datetime,
    created_at: datetime,
    calendar_session_id: str,
    policy: SettlementPolicy,
    provenance: SettlementProvenance,
) -> CashSettlementEvent:
    identity = {
        "run_id": provenance.run_id,
        "arm_id": arm_id,
        "event_type": event_type,
        "receivable_id": receivable_id,
        "source_fill_id": source_fill_id,
        "settled_delta": settled_delta,
        "unsettled_delta": unsettled_delta,
        "effective_at": effective_at,
        "policy_version": policy.version,
        "source_cycle_id": provenance.source_cycle_id,
    }
    event_id = stable_id("q1-cash-event", identity)
    content = {
        **identity,
        "gross_amount": gross_amount,
        "commission": commission,
        "trade_at": trade_at,
        "settlement_date": (
            None if settlement_date is None else settlement_date.isoformat()
        ),
        "calendar_session_id": calendar_session_id,
        "config_manifest_hash": provenance.config_manifest_hash,
        "code_version": provenance.code_version,
        "model_version": provenance.model_version,
        "source_manifest_hash": provenance.source_manifest_hash,
    }
    return CashSettlementEvent(
        cash_settlement_event_id=event_id,
        run_id=provenance.run_id,
        arm_id=arm_id,
        event_type=event_type,
        receivable_id=receivable_id,
        source_fill_id=source_fill_id,
        settlement_policy_version=policy.version,
        settled_cash_delta_usd=settled_delta,
        unsettled_receivable_delta_usd=unsettled_delta,
        gross_amount_usd=gross_amount,
        commission_usd=commission,
        trade_at=trade_at,
        settlement_date=settlement_date,
        effective_at=effective_at,
        calendar_session_id=calendar_session_id,
        source_cycle_id=provenance.source_cycle_id,
        worker_fence_token=provenance.worker_fence_token,
        cycle_attempt_count=provenance.cycle_attempt_count,
        idempotency_key=stable_id("q1-cash-idem", event_id),
        event_hash=canonical_hash(content),
        created_at=created_at,
        config_manifest_hash=provenance.config_manifest_hash,
        code_version=provenance.code_version,
        model_version=provenance.model_version,
        source_manifest_hash=provenance.source_manifest_hash,
    )


def _validate_policy_calendar(
    policy: SettlementPolicy,
    calendar: BusinessCalendar,
) -> None:
    if policy.calendar_version != calendar.version:
        raise SettlementError("Settlement policy and calendar versions differ")


def _validate_unique_events(events: tuple[CashSettlementEvent, ...]) -> None:
    ids = [event.cash_settlement_event_id for event in events]
    idempotency_keys = [event.idempotency_key for event in events]
    if len(ids) != len(set(ids)):
        raise SettlementError("Duplicate cash-settlement event ID")
    if len(idempotency_keys) != len(set(idempotency_keys)):
        raise SettlementError("Duplicate cash-settlement idempotency key")


_EVENT_ORDER = {
    CashSettlementEventType.OPENING_SETTLED_CASH: 0,
    CashSettlementEventType.BUY_SETTLED_CASH_DEBIT: 1,
    CashSettlementEventType.SELL_RECEIVABLE_CREATED: 2,
    CashSettlementEventType.RECEIVABLE_SETTLED: 3,
}
