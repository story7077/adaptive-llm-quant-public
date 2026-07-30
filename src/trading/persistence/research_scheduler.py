from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    and_,
    event,
    func,
    or_,
    select,
)
from sqlalchemy.orm import (
    Mapped,
    Session,
    aliased,
    mapped_column,
    sessionmaker,
)

from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.persistence.models import (
    AppendOnlyViolation,
    Base,
    MarketCalendarSessionRow,
    ResearchEvidenceSourceRow,
)
from trading.research.dispatch_execution import (
    ResearchWorkExecutionResultV1,
)
from trading.research.scheduler import (
    ResearchEvidenceMarker,
    ResearchScheduleEventType,
    ResearchSchedulePlanV1,
    ResearchScheduleWorkKind,
    ResearchWorkDispatchReceiptV1,
    ResearchWorkExecutionLeaseV1,
    ResearchWorkLeaseV1,
    VersionedResearchMarketSession,
    build_dispatch_receipt,
    schedule_plan_json_payload,
)


class ResearchScheduleWorkItemRow(Base):
    __tablename__ = "research_schedule_work_items"
    __table_args__ = (
        CheckConstraint(
            "real_order_routing = false",
            name="ck_research_schedule_work_paper_only",
        ),
    )

    work_item_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(50))
    work_kind: Mapped[str] = mapped_column(String(50))
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    schedule_version: Mapped[str] = mapped_column(String(80))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_available_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True)
    )
    calendar_session_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "market_calendar_sessions.calendar_session_id",
            ondelete="RESTRICT",
        )
    )
    trigger_manifest_hash: Mapped[str] = mapped_column(String(64))
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    plan_hash: Mapped[str] = mapped_column(String(64), unique=True)
    real_order_routing: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchScheduleEventRow(Base):
    __tablename__ = "research_schedule_events"
    __table_args__ = (
        UniqueConstraint(
            "work_item_id",
            "sequence",
            name="uq_research_schedule_event_sequence",
        ),
        UniqueConstraint(
            "work_item_id",
            "idempotency_key",
            name="uq_research_schedule_event_idempotency",
        ),
        CheckConstraint(
            "sequence >= 1",
            name="ck_research_schedule_event_sequence",
        ),
        CheckConstraint(
            "attempt_number >= 0",
            name="ck_research_schedule_event_attempt",
        ),
        CheckConstraint(
            "real_order_routing = false",
            name="ck_research_schedule_event_paper_only",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey(
            "research_schedule_work_items.work_item_id",
            ondelete="RESTRICT",
        )
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(40))
    attempt_number: Mapped[int] = mapped_column(Integer)
    retryable: Mapped[bool] = mapped_column(Boolean)
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_token: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "research_work_dispatch_receipts.receipt_id",
            ondelete="RESTRICT",
        )
    )
    idempotency_key: Mapped[str] = mapped_column(String(160))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    real_order_routing: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchWorkDispatchReceiptRow(Base):
    __tablename__ = "research_work_dispatch_receipts"
    __table_args__ = (
        UniqueConstraint(
            "work_item_id",
            "attempt_number",
            name="uq_research_dispatch_work_attempt",
        ),
        CheckConstraint(
            "attempt_number >= 1",
            name="ck_research_dispatch_attempt",
        ),
        CheckConstraint(
            "real_order_routing = false",
            name="ck_research_dispatch_paper_only",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey(
            "research_schedule_work_items.work_item_id",
            ondelete="RESTRICT",
        )
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    lease_token: Mapped[str] = mapped_column(String(100))
    dispatch_target: Mapped[str] = mapped_column(String(80))
    work_payload_hash: Mapped[str] = mapped_column(String(64))
    config_manifest_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    receipt_hash: Mapped[str] = mapped_column(String(64), unique=True)
    real_order_routing: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


SCHEDULER_APPEND_ONLY_MODEL_TYPES = (
    ResearchScheduleWorkItemRow,
    ResearchScheduleEventRow,
    ResearchWorkDispatchReceiptRow,
)


@event.listens_for(Session, "before_flush")
def prevent_research_scheduler_orm_mutation(
    session: Session,
    *_: object,
) -> None:
    for item in session.dirty:
        if isinstance(
            item,
            SCHEDULER_APPEND_ONLY_MODEL_TYPES,
        ) and session.is_modified(item, include_collections=False):
            raise AppendOnlyViolation(f"{type(item).__name__} is append-only")
    for item in session.deleted:
        if isinstance(item, SCHEDULER_APPEND_ONLY_MODEL_TYPES):
            raise AppendOnlyViolation(f"{type(item).__name__} is append-only")


class ResearchSchedulerPersistenceError(RuntimeError):
    pass


class ResearchScheduleFenceError(ResearchSchedulerPersistenceError):
    pass


class ResearchSchedulerRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def planning_inputs(
        self,
        *,
        as_of: datetime,
        calendar_version: str,
    ) -> tuple[
        tuple[VersionedResearchMarketSession, ...],
        tuple[ResearchEvidenceMarker, ...],
        frozenset[str],
    ]:
        cutoff = require_aware_utc(as_of)
        with self._session_factory() as session:
            calendar_rows = tuple(
                session.scalars(
                    select(MarketCalendarSessionRow)
                    .where(
                        MarketCalendarSessionRow.calendar_version
                        == calendar_version,
                        MarketCalendarSessionRow.available_at <= cutoff,
                    )
                    .order_by(
                        MarketCalendarSessionRow.session_date,
                        MarketCalendarSessionRow.available_at,
                        MarketCalendarSessionRow.calendar_session_id,
                    )
                )
            )
            evidence_rows = tuple(
                session.scalars(
                    select(ResearchEvidenceSourceRow)
                    .where(
                        ResearchEvidenceSourceRow.first_available_at <= cutoff,
                        ResearchEvidenceSourceRow.captured_at <= cutoff,
                    )
                    .order_by(
                        ResearchEvidenceSourceRow.captured_at,
                        ResearchEvidenceSourceRow.first_available_at,
                        ResearchEvidenceSourceRow.content_hash,
                        ResearchEvidenceSourceRow.source_id,
                    )
                )
            )
            evidence_work = tuple(
                session.scalars(
                    select(ResearchScheduleWorkItemRow).where(
                        ResearchScheduleWorkItemRow.work_kind
                        == ResearchScheduleWorkKind.EVIDENCE_TRIGGERED_RESEARCH.value
                    )
                )
            )
        sessions = tuple(
            VersionedResearchMarketSession(
                calendar_session_id=row.calendar_session_id,
                calendar_version=row.calendar_version,
                session_date=row.session_date,
                open_at=_aware(row.open_at),
                close_at=_aware(row.close_at),
                available_at=_aware(row.available_at),
                session_hash=row.session_hash,
            )
            for row in calendar_rows
        )
        evidence = tuple(
            ResearchEvidenceMarker(
                source_id=row.source_id,
                content_hash=row.content_hash,
                first_available_at=_aware(row.first_available_at),
                captured_at=_aware(row.captured_at),
            )
            for row in evidence_rows
        )
        consumed: set[str] = set()
        for row in evidence_work:
            plan = _plan_from_row(row)
            consumed.update(plan.trigger_content_hashes)
        return sessions, evidence, frozenset(consumed)

    def store_plans(
        self,
        plans: tuple[ResearchSchedulePlanV1, ...],
        *,
        created_at: datetime,
    ) -> int:
        instant = require_aware_utc(created_at)
        created = 0
        for plan in plans:
            with self._session_factory.begin() as session:
                existing = session.get(
                    ResearchScheduleWorkItemRow,
                    plan.work_item_id,
                )
                plan_payload = schedule_plan_json_payload(plan)
                if existing is not None:
                    if (
                        existing.plan_hash != plan.plan_hash
                        or existing.payload_json != plan_payload
                    ):
                        raise ResearchSchedulerPersistenceError(
                            "schedule work identity has different immutable content"
                        )
                    continue
                session.add(
                    ResearchScheduleWorkItemRow(
                        work_item_id=plan.work_item_id,
                        schema_version=plan.schema_version,
                        work_kind=plan.work_kind.value,
                        idempotency_key=plan.idempotency_key,
                        schedule_version=plan.schedule_version,
                        scheduled_for=plan.scheduled_for,
                        data_available_cutoff=plan.data_available_cutoff,
                        calendar_session_id=plan.calendar_session_id,
                        trigger_manifest_hash=plan.trigger_manifest_hash,
                        config_manifest_hash=plan.config_manifest_hash,
                        plan_hash=plan.plan_hash,
                        real_order_routing=False,
                        payload_json=plan_payload,
                        created_at=instant,
                    )
                )
                session.flush()
                self._append_event(
                    session,
                    plan=plan,
                    sequence=1,
                    event_type=ResearchScheduleEventType.PLANNED,
                    attempt_number=0,
                    retryable=True,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    receipt_id=None,
                    idempotency_key=f"{plan.idempotency_key}:planned",
                    created_at=instant,
                    extra_payload={"plan_hash": plan.plan_hash},
                )
                created += 1
        return created

    def claim_next(
        self,
        *,
        lease_owner: str,
        lease_seconds: int,
        maximum_attempts: int,
    ) -> ResearchWorkLeaseV1 | None:
        if not lease_owner:
            raise ValueError("lease_owner is required")
        if lease_seconds <= 0 or maximum_attempts <= 0:
            raise ValueError("lease and attempt limits must be positive")
        while True:
            with self._session_factory.begin() as session:
                database_now = _database_now(session)
                ranked = (
                    select(
                        ResearchScheduleEventRow.work_item_id.label(
                            "work_item_id"
                        ),
                        ResearchScheduleEventRow.event_type.label("event_type"),
                        ResearchScheduleEventRow.attempt_number.label(
                            "attempt_number"
                        ),
                        ResearchScheduleEventRow.retryable.label("retryable"),
                        ResearchScheduleEventRow.lease_expires_at.label(
                            "lease_expires_at"
                        ),
                        func.row_number()
                        .over(
                            partition_by=ResearchScheduleEventRow.work_item_id,
                            order_by=(
                                ResearchScheduleEventRow.sequence.desc(),
                                ResearchScheduleEventRow.event_id.desc(),
                            ),
                        )
                        .label("event_rank"),
                    )
                    .subquery()
                )
                latest = (
                    select(
                        ranked.c.work_item_id,
                        ranked.c.event_type,
                        ranked.c.attempt_number,
                        ranked.c.retryable,
                        ranked.c.lease_expires_at,
                    )
                    .where(ranked.c.event_rank == 1)
                    .subquery()
                )
                statement = (
                    select(
                        ResearchScheduleWorkItemRow,
                        latest.c.event_type,
                        latest.c.attempt_number,
                        latest.c.lease_expires_at,
                    )
                    .join(
                        latest,
                        latest.c.work_item_id
                        == ResearchScheduleWorkItemRow.work_item_id,
                    )
                    .where(
                        ResearchScheduleWorkItemRow.scheduled_for
                        <= database_now,
                        latest.c.retryable.is_(True),
                        or_(
                            latest.c.event_type.in_(
                                (
                                    ResearchScheduleEventType.PLANNED.value,
                                    ResearchScheduleEventType.FAILED.value,
                                )
                            ),
                            and_(
                                latest.c.event_type.in_(
                                    (
                                        ResearchScheduleEventType.LEASE_ACQUIRED.value,
                                        ResearchScheduleEventType.LEASE_RECLAIMED.value,
                                    )
                                ),
                                latest.c.lease_expires_at <= database_now,
                            ),
                        ),
                        _schedule_prerequisite_predicate(latest),
                    )
                    .order_by(
                        ResearchScheduleWorkItemRow.scheduled_for,
                        ResearchScheduleWorkItemRow.work_kind,
                        ResearchScheduleWorkItemRow.work_item_id,
                    )
                    .limit(1)
                )
                if (
                    session.bind is not None
                    and session.bind.dialect.name == "postgresql"
                ):
                    statement = statement.with_for_update(
                        of=ResearchScheduleWorkItemRow,
                        skip_locked=True,
                    )
                selected = session.execute(statement).first()
                if selected is None:
                    return None
                row = selected[0]
                previous_type = ResearchScheduleEventType(str(selected[1]))
                previous_attempt = int(selected[2])
                next_attempt = previous_attempt + 1
                plan = _plan_from_row(row)
                latest_event = self._latest_event(
                    session,
                    row.work_item_id,
                )
                if latest_event is None:
                    raise ResearchSchedulerPersistenceError(
                        "planned research work has no event"
                    )
                if next_attempt > maximum_attempts:
                    self._append_event(
                        session,
                        plan=plan,
                        sequence=latest_event.sequence + 1,
                        event_type=ResearchScheduleEventType.FAILED,
                        attempt_number=previous_attempt,
                        retryable=False,
                        lease_owner=None,
                        lease_token=None,
                        lease_expires_at=None,
                        receipt_id=None,
                        idempotency_key=(
                            f"{row.idempotency_key}:attempts-exhausted"
                        ),
                        created_at=database_now,
                        extra_payload={
                            "reason_code": "MAXIMUM_ATTEMPTS_EXHAUSTED",
                            "previous_event_hash": latest_event.event_hash,
                        },
                    )
                    continue
                lease_expires_at = database_now + timedelta(
                    seconds=lease_seconds
                )
                lease_token = stable_id(
                    "research-lease",
                    row.work_item_id,
                    next_attempt,
                    lease_owner,
                    database_now,
                )
                event_type = (
                    ResearchScheduleEventType.LEASE_RECLAIMED
                    if previous_type
                    in {
                        ResearchScheduleEventType.LEASE_ACQUIRED,
                        ResearchScheduleEventType.LEASE_RECLAIMED,
                    }
                    else ResearchScheduleEventType.LEASE_ACQUIRED
                )
                self._append_event(
                    session,
                    plan=plan,
                    sequence=latest_event.sequence + 1,
                    event_type=event_type,
                    attempt_number=next_attempt,
                    retryable=True,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                    receipt_id=None,
                    idempotency_key=(
                        f"{row.idempotency_key}:lease:{next_attempt}"
                    ),
                    created_at=database_now,
                    extra_payload={
                        "previous_event_hash": latest_event.event_hash,
                    },
                )
                return ResearchWorkLeaseV1(
                    work_item_id=row.work_item_id,
                    work_kind=plan.work_kind,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    attempt_number=next_attempt,
                    acquired_at=database_now,
                    lease_expires_at=lease_expires_at,
                    config_manifest_hash=plan.config_manifest_hash,
                    plan_hash=plan.plan_hash,
                    real_order_routing=False,
                )

    def plan_for_lease(
        self,
        lease: ResearchWorkLeaseV1,
    ) -> ResearchSchedulePlanV1:
        with self._session_factory() as session:
            row = session.get(
                ResearchScheduleWorkItemRow,
                lease.work_item_id,
            )
            if row is None:
                raise ResearchSchedulerPersistenceError(
                    f"unknown research work: {lease.work_item_id}"
                )
            plan = _plan_from_row(row)
        if (
            plan.plan_hash != lease.plan_hash
            or plan.config_manifest_hash != lease.config_manifest_hash
        ):
            raise ResearchScheduleFenceError(
                "research lease is not bound to the immutable work plan"
            )
        return plan

    def commit_dispatch(
        self,
        *,
        lease: ResearchWorkLeaseV1,
    ) -> ResearchWorkDispatchReceiptV1:
        with self._session_factory.begin() as session:
            existing = session.scalar(
                select(ResearchWorkDispatchReceiptRow).where(
                    ResearchWorkDispatchReceiptRow.work_item_id
                    == lease.work_item_id,
                    ResearchWorkDispatchReceiptRow.attempt_number
                    == lease.attempt_number,
                )
            )
            if existing is not None:
                receipt = _receipt_from_row(existing)
                if receipt.lease_token != lease.lease_token:
                    raise ResearchScheduleFenceError(
                        "dispatch attempt belongs to another lease token"
                    )
                return receipt
            row = _locked_work(session, lease.work_item_id)
            if row is None:
                raise ResearchSchedulerPersistenceError(
                    f"unknown research work: {lease.work_item_id}"
                )
            plan = _plan_from_row(row)
            database_now = _database_now(session)
            latest = self._require_active_lease(
                session,
                lease=lease,
                database_now=database_now,
            )
            receipt = build_dispatch_receipt(
                plan=plan,
                lease=lease,
                created_at=database_now,
            )
            session.add(
                ResearchWorkDispatchReceiptRow(
                    receipt_id=receipt.receipt_id,
                    work_item_id=receipt.work_item_id,
                    attempt_number=receipt.attempt_number,
                    lease_token=receipt.lease_token,
                    dispatch_target=receipt.dispatch_target.value,
                    work_payload_hash=receipt.work_payload_hash,
                    config_manifest_hash=receipt.config_manifest_hash,
                    idempotency_key=(
                        f"{row.idempotency_key}:dispatch:"
                        f"{receipt.attempt_number}"
                    ),
                    receipt_hash=receipt.receipt_hash,
                    real_order_routing=False,
                    payload_json=receipt.model_dump(mode="json"),
                    created_at=database_now,
                )
            )
            session.flush()
            self._append_event(
                session,
                plan=plan,
                sequence=latest.sequence + 1,
                event_type=ResearchScheduleEventType.DISPATCHED,
                attempt_number=lease.attempt_number,
                retryable=False,
                lease_owner=lease.lease_owner,
                lease_token=lease.lease_token,
                lease_expires_at=lease.lease_expires_at,
                receipt_id=receipt.receipt_id,
                idempotency_key=(
                    f"{row.idempotency_key}:dispatched:"
                    f"{receipt.attempt_number}"
                ),
                created_at=database_now,
                extra_payload={
                    "receipt_hash": receipt.receipt_hash,
                    "dispatch_target": receipt.dispatch_target.value,
                    "previous_event_hash": latest.event_hash,
                },
            )
            return receipt

    def fail_dispatch(
        self,
        *,
        lease: ResearchWorkLeaseV1,
        reason_code: str,
        maximum_attempts: int,
    ) -> bool:
        safe_code = _safe_reason_code(reason_code)
        idempotency_key = (
            f"{lease.work_item_id}:dispatch-failed:{lease.attempt_number}:"
            f"{safe_code}"
        )
        with self._session_factory.begin() as session:
            existing = session.scalar(
                select(ResearchScheduleEventRow).where(
                    ResearchScheduleEventRow.idempotency_key
                    == idempotency_key
                )
            )
            if existing is not None:
                return False
            row = _locked_work(session, lease.work_item_id)
            if row is None:
                raise ResearchSchedulerPersistenceError(
                    f"unknown research work: {lease.work_item_id}"
                )
            database_now = _database_now(session)
            latest = self._require_active_lease(
                session,
                lease=lease,
                database_now=database_now,
            )
            self._append_event(
                session,
                plan=_plan_from_row(row),
                sequence=latest.sequence + 1,
                event_type=ResearchScheduleEventType.FAILED,
                attempt_number=lease.attempt_number,
                retryable=lease.attempt_number < maximum_attempts,
                lease_owner=lease.lease_owner,
                lease_token=lease.lease_token,
                lease_expires_at=lease.lease_expires_at,
                receipt_id=None,
                idempotency_key=idempotency_key,
                created_at=database_now,
                extra_payload={
                    "failure_stage": "DISPATCH_PREPARATION",
                    "reason_code": safe_code,
                    "previous_event_hash": latest.event_hash,
                },
            )
            return True

    def claim_execution(
        self,
        *,
        consumer_id: str,
        lease_seconds: int,
        work_not_before: datetime,
    ) -> ResearchWorkExecutionLeaseV1 | None:
        if not consumer_id:
            raise ValueError("consumer_id is required")
        if lease_seconds <= 0:
            raise ValueError("execution lease must be positive")
        work_not_before = require_aware_utc(
            work_not_before,
            "work_not_before",
        )
        with self._session_factory.begin() as session:
            database_now = _database_now(session)
            ranked = (
                select(
                    ResearchScheduleEventRow.work_item_id.label(
                        "work_item_id"
                    ),
                    ResearchScheduleEventRow.event_type.label("event_type"),
                    ResearchScheduleEventRow.lease_expires_at.label(
                        "lease_expires_at"
                    ),
                    ResearchScheduleEventRow.receipt_id.label("receipt_id"),
                    func.row_number()
                    .over(
                        partition_by=ResearchScheduleEventRow.work_item_id,
                        order_by=(
                            ResearchScheduleEventRow.sequence.desc(),
                            ResearchScheduleEventRow.event_id.desc(),
                        ),
                    )
                    .label("event_rank"),
                )
                .subquery()
            )
            latest = (
                select(
                    ranked.c.work_item_id,
                    ranked.c.event_type,
                    ranked.c.lease_expires_at,
                    ranked.c.receipt_id,
                )
                .where(ranked.c.event_rank == 1)
                .subquery()
            )
            execution_lease_events = (
                ResearchScheduleEventType.EXECUTION_LEASE_ACQUIRED.value,
                ResearchScheduleEventType.EXECUTION_LEASE_RECLAIMED.value,
                ResearchScheduleEventType.EXECUTION_LEASE_RENEWED.value,
            )
            statement = (
                select(
                    ResearchScheduleWorkItemRow,
                    ResearchWorkDispatchReceiptRow,
                )
                .join(
                    latest,
                    latest.c.work_item_id
                    == ResearchScheduleWorkItemRow.work_item_id,
                )
                .join(
                    ResearchWorkDispatchReceiptRow,
                    ResearchWorkDispatchReceiptRow.receipt_id
                    == latest.c.receipt_id,
                )
                .where(
                    or_(
                        latest.c.event_type
                        == ResearchScheduleEventType.DISPATCHED.value,
                        and_(
                            latest.c.event_type.in_(execution_lease_events),
                            latest.c.lease_expires_at <= database_now,
                        ),
                    )
                )
                .where(
                    ResearchScheduleWorkItemRow.scheduled_for
                    >= work_not_before
                )
                .order_by(
                    ResearchScheduleWorkItemRow.scheduled_for,
                    ResearchScheduleWorkItemRow.work_kind,
                    ResearchScheduleWorkItemRow.work_item_id,
                )
                .limit(1)
            )
            if (
                session.bind is not None
                and session.bind.dialect.name == "postgresql"
            ):
                statement = statement.with_for_update(
                    of=ResearchScheduleWorkItemRow,
                    skip_locked=True,
                )
            selected = session.execute(statement).first()
            if selected is None:
                return None
            work = selected[0]
            receipt_row = selected[1]
            plan = _plan_from_row(work)
            receipt = _receipt_from_row(receipt_row)
            latest_event = self._latest_event(session, work.work_item_id)
            if (
                latest_event is None
                or latest_event.receipt_id != receipt.receipt_id
            ):
                raise ResearchScheduleFenceError(
                    "research execution receipt is stale"
                )
            reclaimed = latest_event.event_type in execution_lease_events
            event_type = (
                ResearchScheduleEventType.EXECUTION_LEASE_RECLAIMED
                if reclaimed
                else ResearchScheduleEventType.EXECUTION_LEASE_ACQUIRED
            )
            execution_id = stable_id(
                "research-work-execution",
                receipt.receipt_id,
            )
            sequence = latest_event.sequence + 1
            execution_token = stable_id(
                "research-execution-lease",
                execution_id,
                sequence,
                consumer_id,
                database_now,
            )
            lease_expires_at = database_now + timedelta(
                seconds=lease_seconds
            )
            self._append_event(
                session,
                plan=plan,
                sequence=sequence,
                event_type=event_type,
                attempt_number=receipt.attempt_number,
                retryable=True,
                lease_owner=consumer_id,
                lease_token=execution_token,
                lease_expires_at=lease_expires_at,
                receipt_id=receipt.receipt_id,
                idempotency_key=(
                    f"{work.idempotency_key}:execution-lease:"
                    f"{receipt.attempt_number}:{sequence}"
                ),
                created_at=database_now,
                extra_payload={
                    "execution_id": execution_id,
                    "receipt_hash": receipt.receipt_hash,
                    "work_not_before": work_not_before.isoformat(),
                    "previous_event_hash": latest_event.event_hash,
                },
            )
            return ResearchWorkExecutionLeaseV1(
                execution_id=execution_id,
                receipt_id=receipt.receipt_id,
                receipt_hash=receipt.receipt_hash,
                work_item_id=work.work_item_id,
                work_kind=plan.work_kind,
                consumer_id=consumer_id,
                execution_token=execution_token,
                dispatch_attempt_number=receipt.attempt_number,
                acquired_at=database_now,
                lease_expires_at=lease_expires_at,
                config_manifest_hash=plan.config_manifest_hash,
                real_order_routing=False,
            )

    def execution_input(
        self,
        *,
        execution_lease: ResearchWorkExecutionLeaseV1,
    ) -> tuple[ResearchSchedulePlanV1, ResearchWorkDispatchReceiptV1]:
        with self._session_factory.begin() as session:
            receipt_row = session.get(
                ResearchWorkDispatchReceiptRow,
                execution_lease.receipt_id,
            )
            if receipt_row is None:
                raise ResearchSchedulerPersistenceError(
                    "unknown dispatch receipt: "
                    f"{execution_lease.receipt_id}"
                )
            work = _locked_work(session, execution_lease.work_item_id)
            if work is None:
                raise ResearchSchedulerPersistenceError(
                    f"unknown research work: {execution_lease.work_item_id}"
                )
            plan = _plan_from_row(work)
            receipt = _receipt_from_row(receipt_row)
            self._require_execution_binding(
                execution_lease=execution_lease,
                plan=plan,
                receipt=receipt,
            )
            self._require_active_execution_lease(
                session,
                lease=execution_lease,
                database_now=_database_now(session),
            )
            return plan, receipt

    def renew_execution_lease(
        self,
        *,
        execution_lease: ResearchWorkExecutionLeaseV1,
        lease_seconds: int,
    ) -> ResearchWorkExecutionLeaseV1:
        if lease_seconds <= 0:
            raise ValueError("execution lease must be positive")
        with self._session_factory.begin() as session:
            receipt_row = session.get(
                ResearchWorkDispatchReceiptRow,
                execution_lease.receipt_id,
            )
            if receipt_row is None:
                raise ResearchSchedulerPersistenceError(
                    "unknown dispatch receipt: "
                    f"{execution_lease.receipt_id}"
                )
            work = _locked_work(session, execution_lease.work_item_id)
            if work is None:
                raise ResearchSchedulerPersistenceError(
                    f"unknown research work: {execution_lease.work_item_id}"
                )
            plan = _plan_from_row(work)
            receipt = _receipt_from_row(receipt_row)
            self._require_execution_binding(
                execution_lease=execution_lease,
                plan=plan,
                receipt=receipt,
            )
            database_now = _database_now(session)
            latest = self._require_active_execution_lease(
                session,
                lease=execution_lease,
                database_now=database_now,
            )
            lease_expires_at = database_now + timedelta(
                seconds=lease_seconds
            )
            self._append_event(
                session,
                plan=plan,
                sequence=latest.sequence + 1,
                event_type=(
                    ResearchScheduleEventType.EXECUTION_LEASE_RENEWED
                ),
                attempt_number=receipt.attempt_number,
                retryable=True,
                lease_owner=execution_lease.consumer_id,
                lease_token=execution_lease.execution_token,
                lease_expires_at=lease_expires_at,
                receipt_id=receipt.receipt_id,
                idempotency_key=(
                    f"{work.idempotency_key}:execution-renewal:"
                    f"{receipt.attempt_number}:{latest.sequence + 1}"
                ),
                created_at=database_now,
                extra_payload={
                    "execution_id": execution_lease.execution_id,
                    "receipt_hash": receipt.receipt_hash,
                    "previous_event_hash": latest.event_hash,
                },
            )
            return execution_lease.model_copy(
                update={"lease_expires_at": lease_expires_at}
            )

    def record_execution_outcome(
        self,
        *,
        execution_lease: ResearchWorkExecutionLeaseV1,
        succeeded: bool,
        reason_code: str | None,
        maximum_attempts: int,
        result: ResearchWorkExecutionResultV1 | None = None,
    ) -> bool:
        if maximum_attempts <= 0:
            raise ValueError("maximum_attempts must be positive")
        if succeeded != (result is not None):
            raise ValueError(
                "successful execution requires exactly one typed result"
            )
        with self._session_factory.begin() as session:
            receipt_row = session.get(
                ResearchWorkDispatchReceiptRow,
                execution_lease.receipt_id,
            )
            if receipt_row is None:
                raise ResearchSchedulerPersistenceError(
                    "unknown dispatch receipt: "
                    f"{execution_lease.receipt_id}"
                )
            work = _locked_work(session, receipt_row.work_item_id)
            if work is None:
                raise ResearchSchedulerPersistenceError(
                    f"unknown research work: {receipt_row.work_item_id}"
                )
            plan = _plan_from_row(work)
            receipt = _receipt_from_row(receipt_row)
            self._require_execution_binding(
                execution_lease=execution_lease,
                plan=plan,
                receipt=receipt,
            )
            if result is not None and (
                result.execution_id != execution_lease.execution_id
                or result.receipt_id != receipt.receipt_id
                or result.work_item_id != plan.work_item_id
                or result.work_kind is not plan.work_kind
                or result.dispatch_target is not receipt.dispatch_target
                or result.real_order_routing
                or result.automatic_promotion_enabled
            ):
                raise ResearchScheduleFenceError(
                    "research execution result binding is invalid"
                )
            latest = self._latest_event(session, work.work_item_id)
            if latest is None:
                raise ResearchSchedulerPersistenceError(
                    "research work has no event history"
                )
            event_type = (
                ResearchScheduleEventType.SUCCEEDED
                if succeeded
                else ResearchScheduleEventType.FAILED
            )
            safe_code = (
                None
                if succeeded
                else _safe_reason_code(reason_code or "EXECUTION_FAILED")
            )
            idempotency_key = (
                f"{work.idempotency_key}:execution:"
                f"{receipt_row.attempt_number}:{event_type.value}:"
                f"{safe_code or 'OK'}"
            )
            existing = session.scalar(
                select(ResearchScheduleEventRow).where(
                    ResearchScheduleEventRow.idempotency_key
                    == idempotency_key
                )
            )
            if existing is not None:
                if (
                    existing.lease_token
                    != execution_lease.execution_token
                ):
                    raise ResearchScheduleFenceError(
                        "research execution outcome belongs to a stale worker"
                    )
                return False
            database_now = _database_now(session)
            latest = self._require_active_execution_lease(
                session,
                lease=execution_lease,
                database_now=database_now,
            )
            if result is not None:
                self._require_unused_invocation_contexts(
                    session,
                    result=result,
                )
            self._append_event(
                session,
                plan=plan,
                sequence=latest.sequence + 1,
                event_type=event_type,
                attempt_number=receipt_row.attempt_number,
                retryable=(
                    not succeeded
                    and receipt_row.attempt_number < maximum_attempts
                ),
                lease_owner=execution_lease.consumer_id,
                lease_token=execution_lease.execution_token,
                lease_expires_at=execution_lease.lease_expires_at,
                receipt_id=execution_lease.receipt_id,
                idempotency_key=idempotency_key,
                created_at=database_now,
                extra_payload={
                    "execution_id": execution_lease.execution_id,
                    "failure_stage": None if succeeded else "WORK_EXECUTION",
                    "reason_code": safe_code,
                    "receipt_hash": receipt_row.receipt_hash,
                    "result_hash": (
                        None if result is None else result.result_hash
                    ),
                    "execution_result": (
                        None
                        if result is None
                        else result.model_dump(mode="json")
                    ),
                    "previous_event_hash": latest.event_hash,
                },
            )
            return True

    def status(
        self,
        *,
        history_limit: int,
        schedule_version: str,
        config_manifest_hash: str,
        timezone: str,
    ) -> dict[str, Any]:
        if history_limit <= 0:
            raise ValueError("history_limit must be positive")
        with self._session_factory() as session:
            database_now = _database_now(session)
            work_rows = tuple(
                session.scalars(
                    select(ResearchScheduleWorkItemRow)
                    .order_by(
                        ResearchScheduleWorkItemRow.scheduled_for.desc(),
                        ResearchScheduleWorkItemRow.work_item_id.desc(),
                    )
                    .limit(history_limit)
                )
            )
            items: list[dict[str, Any]] = []
            counts: dict[str, int] = {}
            for row in work_rows:
                latest = self._latest_event(session, row.work_item_id)
                if latest is None:
                    status = "CORRUPT_MISSING_EVENT"
                else:
                    status = _projected_status(latest, database_now)
                counts[status] = counts.get(status, 0) + 1
                items.append(
                    {
                        "work_item_id": row.work_item_id,
                        "work_kind": row.work_kind,
                        "scheduled_for": _iso(row.scheduled_for),
                        "data_available_cutoff": _iso(
                            row.data_available_cutoff
                        ),
                        "calendar_session_id": row.calendar_session_id,
                        "plan_hash": row.plan_hash,
                        "status": status,
                        "attempt_number": (
                            0 if latest is None else latest.attempt_number
                        ),
                        "latest_event_type": (
                            None if latest is None else latest.event_type
                        ),
                        "latest_event_at": (
                            None
                            if latest is None
                            else _iso(latest.created_at)
                        ),
                        "receipt_id": (
                            None if latest is None else latest.receipt_id
                        ),
                    }
                )
        return {
            "schedule_version": schedule_version,
            "config_manifest_hash": config_manifest_hash,
            "timezone": timezone,
            "database_clock_as_of": _iso(database_now),
            "recent_status_counts": counts,
            "recent_work": items,
            "real_order_routing": False,
        }

    def event_history(
        self,
        work_item_id: str,
    ) -> tuple[ResearchScheduleEventRow, ...]:
        with self._session_factory() as session:
            return tuple(
                session.scalars(
                    select(ResearchScheduleEventRow)
                    .where(
                        ResearchScheduleEventRow.work_item_id == work_item_id
                    )
                    .order_by(
                        ResearchScheduleEventRow.sequence,
                        ResearchScheduleEventRow.event_id,
                    )
                )
            )

    def _require_active_lease(
        self,
        session: Session,
        *,
        lease: ResearchWorkLeaseV1,
        database_now: datetime,
    ) -> ResearchScheduleEventRow:
        latest = self._latest_event(session, lease.work_item_id)
        if (
            latest is None
            or latest.event_type
            not in {
                ResearchScheduleEventType.LEASE_ACQUIRED.value,
                ResearchScheduleEventType.LEASE_RECLAIMED.value,
            }
            or latest.lease_owner != lease.lease_owner
            or latest.lease_token != lease.lease_token
            or latest.attempt_number != lease.attempt_number
            or latest.lease_expires_at is None
            or _aware(latest.lease_expires_at) <= database_now
        ):
            raise ResearchScheduleFenceError(
                "research work lease is stale, expired, or reclaimed"
            )
        return latest

    def _require_active_execution_lease(
        self,
        session: Session,
        *,
        lease: ResearchWorkExecutionLeaseV1,
        database_now: datetime,
    ) -> ResearchScheduleEventRow:
        latest = self._latest_event(session, lease.work_item_id)
        if (
            latest is None
            or latest.event_type
            not in {
                ResearchScheduleEventType
                .EXECUTION_LEASE_ACQUIRED.value,
                ResearchScheduleEventType
                .EXECUTION_LEASE_RECLAIMED.value,
                ResearchScheduleEventType
                .EXECUTION_LEASE_RENEWED.value,
            }
            or latest.lease_owner != lease.consumer_id
            or latest.lease_token != lease.execution_token
            or latest.attempt_number != lease.dispatch_attempt_number
            or latest.receipt_id != lease.receipt_id
            or latest.lease_expires_at is None
            or _aware(latest.lease_expires_at) <= database_now
        ):
            raise ResearchScheduleFenceError(
                "research execution lease is stale, expired, or reclaimed"
            )
        return latest

    @staticmethod
    def _require_execution_binding(
        *,
        execution_lease: ResearchWorkExecutionLeaseV1,
        plan: ResearchSchedulePlanV1,
        receipt: ResearchWorkDispatchReceiptV1,
    ) -> None:
        if (
            execution_lease.work_item_id != plan.work_item_id
            or execution_lease.work_item_id != receipt.work_item_id
            or execution_lease.work_kind is not plan.work_kind
            or execution_lease.work_kind is not receipt.work_kind
            or execution_lease.receipt_id != receipt.receipt_id
            or execution_lease.receipt_hash != receipt.receipt_hash
            or execution_lease.dispatch_attempt_number
            != receipt.attempt_number
            or execution_lease.config_manifest_hash
            != plan.config_manifest_hash
            or execution_lease.config_manifest_hash
            != receipt.config_manifest_hash
            or execution_lease.real_order_routing
        ):
            raise ResearchScheduleFenceError(
                "research execution lease binding is invalid"
            )

    @staticmethod
    def _require_unused_invocation_contexts(
        session: Session,
        *,
        result: ResearchWorkExecutionResultV1,
    ) -> None:
        incoming = {
            item.invocation_context_hash for item in result.invocations
        }
        if not incoming:
            return
        succeeded_rows = session.scalars(
            select(ResearchScheduleEventRow).where(
                ResearchScheduleEventRow.event_type
                == ResearchScheduleEventType.SUCCEEDED.value
            )
        )
        used: set[str] = set()
        for row in succeeded_rows:
            stored = row.payload_json.get("execution_result")
            if not isinstance(stored, dict):
                continue
            try:
                existing = ResearchWorkExecutionResultV1.model_validate(
                    stored
                )
            except ValueError as exc:
                raise ResearchSchedulerPersistenceError(
                    "stored research execution result is invalid"
                ) from exc
            used.update(
                item.invocation_context_hash
                for item in existing.invocations
            )
        if incoming & used:
            raise ResearchScheduleFenceError(
                "research execution reused an earlier model context"
            )

    @staticmethod
    def _latest_event(
        session: Session,
        work_item_id: str,
    ) -> ResearchScheduleEventRow | None:
        return session.scalar(
            select(ResearchScheduleEventRow)
            .where(ResearchScheduleEventRow.work_item_id == work_item_id)
            .order_by(
                ResearchScheduleEventRow.sequence.desc(),
                ResearchScheduleEventRow.event_id.desc(),
            )
            .limit(1)
        )

    @staticmethod
    def _append_event(
        session: Session,
        *,
        plan: ResearchSchedulePlanV1,
        sequence: int,
        event_type: ResearchScheduleEventType,
        attempt_number: int,
        retryable: bool,
        lease_owner: str | None,
        lease_token: str | None,
        lease_expires_at: datetime | None,
        receipt_id: str | None,
        idempotency_key: str,
        created_at: datetime,
        extra_payload: dict[str, Any],
    ) -> ResearchScheduleEventRow:
        normalized_created_at = _aware(created_at)
        normalized_lease_expires_at = (
            None
            if lease_expires_at is None
            else _aware(lease_expires_at)
        )
        event_id = stable_id(
            "research-schedule-event",
            plan.work_item_id,
            sequence,
            event_type,
            attempt_number,
        )
        payload = {
            "event_id": event_id,
            "work_item_id": plan.work_item_id,
            "sequence": sequence,
            "event_type": event_type.value,
            "attempt_number": attempt_number,
            "retryable": retryable,
            "lease_owner": lease_owner,
            "lease_token": lease_token,
            "lease_expires_at": normalized_lease_expires_at,
            "receipt_id": receipt_id,
            "idempotency_key": idempotency_key,
            "config_manifest_hash": plan.config_manifest_hash,
            "real_order_routing": False,
            "created_at": normalized_created_at,
            **extra_payload,
        }
        row = ResearchScheduleEventRow(
            event_id=event_id,
            work_item_id=plan.work_item_id,
            sequence=sequence,
            event_type=event_type.value,
            attempt_number=attempt_number,
            retryable=retryable,
            lease_owner=lease_owner,
            lease_token=lease_token,
            lease_expires_at=normalized_lease_expires_at,
            receipt_id=receipt_id,
            idempotency_key=idempotency_key,
            event_hash=canonical_hash(payload),
            config_manifest_hash=plan.config_manifest_hash,
            real_order_routing=False,
            payload_json=_json_payload(payload),
            created_at=normalized_created_at,
        )
        session.add(row)
        return row


def _locked_work(
    session: Session,
    work_item_id: str,
) -> ResearchScheduleWorkItemRow | None:
    statement = select(ResearchScheduleWorkItemRow).where(
        ResearchScheduleWorkItemRow.work_item_id == work_item_id
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    return session.scalar(statement)


def _schedule_prerequisite_predicate(latest: Any) -> Any:
    def predecessor_succeeded(
        predecessor_kind: ResearchScheduleWorkKind,
        *,
        alias_name: str,
    ) -> Any:
        predecessor = aliased(
            ResearchScheduleWorkItemRow,
            name=f"{alias_name}_work",
        )
        predecessor_latest = latest.alias(f"{alias_name}_latest")
        return (
            select(predecessor.work_item_id)
            .join(
                predecessor_latest,
                predecessor_latest.c.work_item_id
                == predecessor.work_item_id,
            )
            .where(
                predecessor.work_kind == predecessor_kind.value,
                predecessor.schedule_version
                == ResearchScheduleWorkItemRow.schedule_version,
                predecessor.calendar_session_id
                == ResearchScheduleWorkItemRow.calendar_session_id,
                predecessor.config_manifest_hash
                == ResearchScheduleWorkItemRow.config_manifest_hash,
                predecessor_latest.c.event_type
                == ResearchScheduleEventType.SUCCEEDED.value,
            )
            .exists()
        )

    dependent_kinds = (
        ResearchScheduleWorkKind.OUTCOME_MATURATION.value,
        ResearchScheduleWorkKind.RESEARCH_MEMORY_MATERIALIZATION.value,
        ResearchScheduleWorkKind.OPERATOR_DEEP_RESEARCH.value,
    )
    return or_(
        ResearchScheduleWorkItemRow.work_kind.not_in(dependent_kinds),
        and_(
            ResearchScheduleWorkItemRow.work_kind
            == ResearchScheduleWorkKind.OUTCOME_MATURATION.value,
            predecessor_succeeded(
                ResearchScheduleWorkKind.DAILY_AGGREGATION,
                alias_name="daily_predecessor",
            ),
        ),
        and_(
            ResearchScheduleWorkItemRow.work_kind
            == (
                ResearchScheduleWorkKind
                .RESEARCH_MEMORY_MATERIALIZATION.value
            ),
            predecessor_succeeded(
                ResearchScheduleWorkKind.OUTCOME_MATURATION,
                alias_name="outcome_predecessor",
            ),
        ),
        and_(
            ResearchScheduleWorkItemRow.work_kind
            == ResearchScheduleWorkKind.OPERATOR_DEEP_RESEARCH.value,
            predecessor_succeeded(
                ResearchScheduleWorkKind.DAILY_AGGREGATION,
                alias_name="operator_daily_predecessor",
            ),
        ),
    )


def _database_now(session: Session) -> datetime:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        value = session.scalar(select(func.clock_timestamp()))
    else:
        value = session.scalar(select(func.current_timestamp()))
    if not isinstance(value, datetime):
        raise ResearchScheduleFenceError("database clock is unavailable")
    return _aware(value)


def _plan_from_row(
    row: ResearchScheduleWorkItemRow,
) -> ResearchSchedulePlanV1:
    plan = ResearchSchedulePlanV1.model_validate(row.payload_json)
    if (
        plan.work_item_id != row.work_item_id
        or plan.plan_hash != row.plan_hash
        or plan.config_manifest_hash != row.config_manifest_hash
        or plan.real_order_routing
        or row.real_order_routing
    ):
        raise ResearchSchedulerPersistenceError(
            "stored research work binding is invalid"
        )
    return plan


def _receipt_from_row(
    row: ResearchWorkDispatchReceiptRow,
) -> ResearchWorkDispatchReceiptV1:
    receipt = ResearchWorkDispatchReceiptV1.model_validate(row.payload_json)
    if (
        receipt.receipt_id != row.receipt_id
        or receipt.receipt_hash != row.receipt_hash
        or receipt.real_order_routing
        or row.real_order_routing
    ):
        raise ResearchSchedulerPersistenceError(
            "stored dispatch receipt binding is invalid"
        )
    return receipt


def _projected_status(
    event_row: ResearchScheduleEventRow,
    database_now: datetime,
) -> str:
    event_type = ResearchScheduleEventType(event_row.event_type)
    if event_type is ResearchScheduleEventType.PLANNED:
        return "PENDING"
    if event_type in {
        ResearchScheduleEventType.LEASE_ACQUIRED,
        ResearchScheduleEventType.LEASE_RECLAIMED,
    }:
        if (
            event_row.lease_expires_at is not None
            and _aware(event_row.lease_expires_at) <= database_now
        ):
            return "LEASE_EXPIRED"
        return "LEASED"
    if event_type is ResearchScheduleEventType.DISPATCHED:
        return "DISPATCHED"
    if event_type in {
        ResearchScheduleEventType.EXECUTION_LEASE_ACQUIRED,
        ResearchScheduleEventType.EXECUTION_LEASE_RECLAIMED,
        ResearchScheduleEventType.EXECUTION_LEASE_RENEWED,
    }:
        if (
            event_row.lease_expires_at is not None
            and _aware(event_row.lease_expires_at) <= database_now
        ):
            return "EXECUTION_LEASE_EXPIRED"
        return "EXECUTING"
    if event_type is ResearchScheduleEventType.SUCCEEDED:
        return "SUCCEEDED"
    return "RETRY_PENDING" if event_row.retryable else "FAILED_TERMINAL"


def _safe_reason_code(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in value.upper()
    ).strip("_")
    if not normalized:
        normalized = "UNSPECIFIED_FAILURE"
    return normalized[:80]


def _json_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            item.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if isinstance(item, datetime)
            else item.value
            if isinstance(item, ResearchScheduleEventType)
            else item
        )
        for key, item in value.items()
    }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat().replace("+00:00", "Z")
