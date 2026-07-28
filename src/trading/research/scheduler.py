from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.research.config import ResearchScheduleConfig

SCHEDULE_SCHEMA_VERSION = "research_schedule_plan_v1"
DISPATCH_RECEIPT_SCHEMA_VERSION = "research_work_dispatch_receipt_v1"


class ResearchScheduleWorkKind(StrEnum):
    DAILY_AGGREGATION = "DAILY_AGGREGATION"
    WEEKLY_DEEP_RESEARCH = "WEEKLY_DEEP_RESEARCH"
    EVIDENCE_TRIGGERED_RESEARCH = "EVIDENCE_TRIGGERED_RESEARCH"


class ResearchScheduleEventType(StrEnum):
    PLANNED = "PLANNED"
    LEASE_ACQUIRED = "LEASE_ACQUIRED"
    LEASE_RECLAIMED = "LEASE_RECLAIMED"
    DISPATCHED = "DISPATCHED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ResearchDispatchTarget(StrEnum):
    DAILY_AGGREGATION_V1 = "RESEARCH_DAILY_AGGREGATION_V1"
    DEEP_RESEARCH_CYCLE_V1 = "RESEARCH_DEEP_CYCLE_V1"


class VersionedResearchMarketSession(DomainModel):
    calendar_session_id: str = Field(min_length=1)
    calendar_version: str = Field(min_length=1)
    session_date: date
    open_at: datetime
    close_at: datetime
    available_at: datetime
    session_hash: str = Field(min_length=1)

    @field_validator("open_at", "close_at", "available_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_session(self) -> Self:
        if self.close_at <= self.open_at:
            raise ValueError("market session close must follow open")
        return self


class ResearchEvidenceMarker(DomainModel):
    source_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    first_available_at: datetime
    captured_at: datetime

    @field_validator("first_available_at", "captured_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.captured_at < self.first_available_at:
            raise ValueError("evidence cannot be captured before first availability")
        return self


class ResearchSchedulePlanV1(DomainModel):
    schema_version: Literal["research_schedule_plan_v1"] = SCHEDULE_SCHEMA_VERSION
    work_item_id: str = Field(min_length=1)
    work_kind: ResearchScheduleWorkKind
    idempotency_key: str = Field(min_length=1)
    schedule_version: str = Field(min_length=1)
    scheduled_for: datetime
    data_available_cutoff: datetime
    calendar_session_id: str | None
    calendar_session_hash: str | None
    calendar_version: str | None
    trigger_source_ids: tuple[str, ...]
    trigger_content_hashes: tuple[str, ...]
    trigger_manifest_hash: str = Field(min_length=1)
    config_manifest_hash: str = Field(min_length=1)
    plan_hash: str = Field(min_length=1)
    real_order_routing: Literal[False] = False

    @field_validator("scheduled_for", "data_available_cutoff", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.data_available_cutoff > self.scheduled_for:
            raise ValueError("schedule data cutoff cannot follow scheduled_for")
        session_fields = (
            self.calendar_session_id,
            self.calendar_session_hash,
            self.calendar_version,
        )
        if self.work_kind in {
            ResearchScheduleWorkKind.DAILY_AGGREGATION,
            ResearchScheduleWorkKind.WEEKLY_DEEP_RESEARCH,
        } and any(value is None for value in session_fields):
            raise ValueError("calendar-backed work requires a versioned session")
        if self.work_kind is ResearchScheduleWorkKind.EVIDENCE_TRIGGERED_RESEARCH:
            if not self.trigger_source_ids or not self.trigger_content_hashes:
                raise ValueError("evidence-triggered work requires bounded evidence")
        elif self.trigger_source_ids or self.trigger_content_hashes:
            raise ValueError("calendar cadence work cannot carry evidence triggers")
        if tuple(sorted(self.trigger_source_ids)) != self.trigger_source_ids:
            raise ValueError("trigger source IDs must be sorted")
        if tuple(sorted(self.trigger_content_hashes)) != self.trigger_content_hashes:
            raise ValueError("trigger content hashes must be sorted")
        if self.trigger_manifest_hash != canonical_hash(
            {
                "source_ids": self.trigger_source_ids,
                "content_hashes": self.trigger_content_hashes,
            }
        ):
            raise ValueError("trigger manifest hash mismatch")
        if self.plan_hash != canonical_hash(_plan_hash_payload(self)):
            raise ValueError("research schedule plan hash mismatch")
        return self


class ResearchWorkLeaseV1(DomainModel):
    work_item_id: str
    work_kind: ResearchScheduleWorkKind
    lease_owner: str
    lease_token: str
    attempt_number: int = Field(gt=0)
    acquired_at: datetime
    lease_expires_at: datetime
    config_manifest_hash: str
    plan_hash: str
    real_order_routing: Literal[False] = False

    @field_validator("acquired_at", "lease_expires_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.lease_expires_at <= self.acquired_at:
            raise ValueError("research work lease must have positive duration")
        return self


class ResearchWorkDispatchReceiptV1(DomainModel):
    schema_version: Literal[
        "research_work_dispatch_receipt_v1"
    ] = DISPATCH_RECEIPT_SCHEMA_VERSION
    receipt_id: str
    work_item_id: str
    work_kind: ResearchScheduleWorkKind
    attempt_number: int = Field(gt=0)
    lease_token: str
    dispatch_target: ResearchDispatchTarget
    work_payload_hash: str
    config_manifest_hash: str
    created_at: datetime
    receipt_hash: str
    real_order_routing: Literal[False] = False

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        if self.receipt_hash != canonical_hash(_receipt_hash_payload(self)):
            raise ValueError("research dispatch receipt hash mismatch")
        return self


def build_due_schedule_plans(
    *,
    schedule: ResearchScheduleConfig,
    config_manifest_hash: str,
    as_of: datetime,
    market_sessions: tuple[VersionedResearchMarketSession, ...],
    evidence: tuple[ResearchEvidenceMarker, ...],
    consumed_evidence_hashes: frozenset[str] = frozenset(),
) -> tuple[ResearchSchedulePlanV1, ...]:
    cutoff = require_aware_utc(as_of)
    timezone = ZoneInfo(schedule.timezone)
    earliest = cutoff - timedelta(days=schedule.planning_lookback_days)
    latest_sessions = _latest_sessions_as_of(
        market_sessions,
        calendar_version=schedule.market_calendar_version,
        as_of=cutoff,
    )
    plans = [
        *_daily_plans(
            schedule=schedule,
            config_manifest_hash=config_manifest_hash,
            as_of=cutoff,
            earliest=earliest,
            timezone=timezone,
            sessions=latest_sessions,
        ),
        *_weekly_plans(
            schedule=schedule,
            config_manifest_hash=config_manifest_hash,
            as_of=cutoff,
            earliest=earliest,
            timezone=timezone,
            sessions=latest_sessions,
        ),
        *_evidence_plans(
            schedule=schedule,
            config_manifest_hash=config_manifest_hash,
            as_of=cutoff,
            evidence=evidence,
            consumed_evidence_hashes=consumed_evidence_hashes,
        ),
    ]
    return tuple(
        sorted(
            plans,
            key=lambda item: (
                item.scheduled_for,
                item.work_kind.value,
                item.work_item_id,
            ),
        )
    )


def dispatch_target_for(
    work_kind: ResearchScheduleWorkKind,
) -> ResearchDispatchTarget:
    if work_kind is ResearchScheduleWorkKind.DAILY_AGGREGATION:
        return ResearchDispatchTarget.DAILY_AGGREGATION_V1
    return ResearchDispatchTarget.DEEP_RESEARCH_CYCLE_V1


def build_dispatch_receipt(
    *,
    plan: ResearchSchedulePlanV1,
    lease: ResearchWorkLeaseV1,
    created_at: datetime,
) -> ResearchWorkDispatchReceiptV1:
    instant = require_aware_utc(created_at)
    if plan.work_item_id != lease.work_item_id or plan.plan_hash != lease.plan_hash:
        raise ValueError("dispatch lease does not match its planned work")
    payload = {
        "schema_version": DISPATCH_RECEIPT_SCHEMA_VERSION,
        "receipt_id": stable_id(
            "research-dispatch",
            plan.work_item_id,
            lease.attempt_number,
        ),
        "work_item_id": plan.work_item_id,
        "work_kind": plan.work_kind,
        "attempt_number": lease.attempt_number,
        "lease_token": lease.lease_token,
        "dispatch_target": dispatch_target_for(plan.work_kind),
        "work_payload_hash": canonical_hash(plan),
        "config_manifest_hash": plan.config_manifest_hash,
        "created_at": instant,
        "real_order_routing": False,
    }
    return ResearchWorkDispatchReceiptV1.model_validate(
        {**payload, "receipt_hash": canonical_hash(payload)}
    )


def _daily_plans(
    *,
    schedule: ResearchScheduleConfig,
    config_manifest_hash: str,
    as_of: datetime,
    earliest: datetime,
    timezone: ZoneInfo,
    sessions: tuple[VersionedResearchMarketSession, ...],
) -> list[ResearchSchedulePlanV1]:
    configured_clock = time.fromisoformat(schedule.daily_aggregation_time)
    plans: list[ResearchSchedulePlanV1] = []
    for session in sessions:
        configured_at = datetime.combine(
            session.session_date,
            configured_clock,
            tzinfo=timezone,
        ).astimezone(UTC)
        after_actual_close = session.close_at + timedelta(
            minutes=schedule.daily_post_close_delay_minutes
        )
        scheduled_for = max(configured_at, after_actual_close)
        if scheduled_for < earliest or scheduled_for > as_of:
            continue
        plans.append(
            _calendar_plan(
                work_kind=ResearchScheduleWorkKind.DAILY_AGGREGATION,
                identity=session.session_date.isoformat(),
                scheduled_for=scheduled_for,
                session=session,
                schedule=schedule,
                config_manifest_hash=config_manifest_hash,
            )
        )
    return plans


def _weekly_plans(
    *,
    schedule: ResearchScheduleConfig,
    config_manifest_hash: str,
    as_of: datetime,
    earliest: datetime,
    timezone: ZoneInfo,
    sessions: tuple[VersionedResearchMarketSession, ...],
) -> list[ResearchSchedulePlanV1]:
    weekday = _weekday_number(schedule.weekly_research_day)
    configured_clock = time.fromisoformat(schedule.weekly_research_time)
    start_date = earliest.astimezone(timezone).date()
    end_date = as_of.astimezone(timezone).date()
    plans: list[ResearchSchedulePlanV1] = []
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() == weekday:
            scheduled_for = datetime.combine(
                cursor,
                configured_clock,
                tzinfo=timezone,
            ).astimezone(UTC)
            if earliest <= scheduled_for <= as_of:
                completed = tuple(
                    item for item in sessions if item.close_at <= scheduled_for
                )
                if completed:
                    anchor = max(
                        completed,
                        key=lambda item: (
                            item.session_date,
                            item.available_at,
                            item.calendar_session_id,
                        ),
                    )
                    plans.append(
                        _calendar_plan(
                            work_kind=(
                                ResearchScheduleWorkKind.WEEKLY_DEEP_RESEARCH
                            ),
                            identity=cursor.isoformat(),
                            scheduled_for=scheduled_for,
                            session=anchor,
                            schedule=schedule,
                            config_manifest_hash=config_manifest_hash,
                        )
                    )
        cursor += timedelta(days=1)
    return plans


def _evidence_plans(
    *,
    schedule: ResearchScheduleConfig,
    config_manifest_hash: str,
    as_of: datetime,
    evidence: tuple[ResearchEvidenceMarker, ...],
    consumed_evidence_hashes: frozenset[str],
) -> list[ResearchSchedulePlanV1]:
    unique: dict[str, ResearchEvidenceMarker] = {}
    for marker in sorted(
        evidence,
        key=lambda item: (
            item.captured_at,
            item.first_available_at,
            item.content_hash,
            item.source_id,
        ),
    ):
        if (
            marker.captured_at > as_of
            or marker.first_available_at > as_of
            or marker.content_hash in consumed_evidence_hashes
        ):
            continue
        unique.setdefault(marker.content_hash, marker)
    ordered = tuple(unique.values())
    threshold = schedule.evidence_trigger_minimum_new_sources
    plans: list[ResearchSchedulePlanV1] = []
    for offset in range(0, len(ordered), threshold):
        batch = ordered[offset : offset + threshold]
        if len(batch) < threshold:
            break
        source_ids = tuple(sorted(item.source_id for item in batch))
        content_hashes = tuple(sorted(item.content_hash for item in batch))
        scheduled_for = max(item.captured_at for item in batch)
        trigger_manifest_hash = canonical_hash(
            {
                "source_ids": source_ids,
                "content_hashes": content_hashes,
            }
        )
        identity = canonical_hash(content_hashes)
        plans.append(
            _make_plan(
                work_kind=(
                    ResearchScheduleWorkKind.EVIDENCE_TRIGGERED_RESEARCH
                ),
                identity=identity,
                scheduled_for=scheduled_for,
                data_available_cutoff=scheduled_for,
                calendar_session_id=None,
                calendar_session_hash=None,
                calendar_version=None,
                trigger_source_ids=source_ids,
                trigger_content_hashes=content_hashes,
                trigger_manifest_hash=trigger_manifest_hash,
                schedule=schedule,
                config_manifest_hash=config_manifest_hash,
            )
        )
    return plans


def _calendar_plan(
    *,
    work_kind: ResearchScheduleWorkKind,
    identity: str,
    scheduled_for: datetime,
    session: VersionedResearchMarketSession,
    schedule: ResearchScheduleConfig,
    config_manifest_hash: str,
) -> ResearchSchedulePlanV1:
    empty_trigger_hash = canonical_hash(
        {"source_ids": (), "content_hashes": ()}
    )
    return _make_plan(
        work_kind=work_kind,
        identity=identity,
        scheduled_for=scheduled_for,
        data_available_cutoff=scheduled_for,
        calendar_session_id=session.calendar_session_id,
        calendar_session_hash=session.session_hash,
        calendar_version=session.calendar_version,
        trigger_source_ids=(),
        trigger_content_hashes=(),
        trigger_manifest_hash=empty_trigger_hash,
        schedule=schedule,
        config_manifest_hash=config_manifest_hash,
    )


def _make_plan(
    *,
    work_kind: ResearchScheduleWorkKind,
    identity: str,
    scheduled_for: datetime,
    data_available_cutoff: datetime,
    calendar_session_id: str | None,
    calendar_session_hash: str | None,
    calendar_version: str | None,
    trigger_source_ids: tuple[str, ...],
    trigger_content_hashes: tuple[str, ...],
    trigger_manifest_hash: str,
    schedule: ResearchScheduleConfig,
    config_manifest_hash: str,
) -> ResearchSchedulePlanV1:
    work_item_id = stable_id(
        "research-work",
        schedule.schedule_version,
        work_kind,
        identity,
    )
    payload = {
        "schema_version": SCHEDULE_SCHEMA_VERSION,
        "work_item_id": work_item_id,
        "work_kind": work_kind,
        "idempotency_key": stable_id(
            "research-work-idempotency",
            schedule.schedule_version,
            work_kind,
            identity,
        ),
        "schedule_version": schedule.schedule_version,
        "scheduled_for": require_aware_utc(scheduled_for),
        "data_available_cutoff": require_aware_utc(data_available_cutoff),
        "calendar_session_id": calendar_session_id,
        "calendar_session_hash": calendar_session_hash,
        "calendar_version": calendar_version,
        "trigger_source_ids": trigger_source_ids,
        "trigger_content_hashes": trigger_content_hashes,
        "trigger_manifest_hash": trigger_manifest_hash,
        "config_manifest_hash": config_manifest_hash,
        "real_order_routing": False,
    }
    return ResearchSchedulePlanV1.model_validate(
        {**payload, "plan_hash": canonical_hash(payload)}
    )


def _latest_sessions_as_of(
    sessions: tuple[VersionedResearchMarketSession, ...],
    *,
    calendar_version: str,
    as_of: datetime,
) -> tuple[VersionedResearchMarketSession, ...]:
    latest: dict[date, VersionedResearchMarketSession] = {}
    for session in sessions:
        if (
            session.calendar_version != calendar_version
            or session.available_at > as_of
        ):
            continue
        current = latest.get(session.session_date)
        if current is None or (
            session.available_at,
            session.calendar_session_id,
        ) > (
            current.available_at,
            current.calendar_session_id,
        ):
            latest[session.session_date] = session
    return tuple(latest[item] for item in sorted(latest))


def _weekday_number(value: str) -> int:
    return {
        "MONDAY": 0,
        "TUESDAY": 1,
        "WEDNESDAY": 2,
        "THURSDAY": 3,
        "FRIDAY": 4,
        "SATURDAY": 5,
        "SUNDAY": 6,
    }[value]


def _plan_hash_payload(plan: ResearchSchedulePlanV1) -> dict[str, object]:
    payload = plan.model_dump(mode="python", exclude={"plan_hash"})
    return payload


def _receipt_hash_payload(
    receipt: ResearchWorkDispatchReceiptV1,
) -> dict[str, object]:
    return receipt.model_dump(mode="python", exclude={"receipt_hash"})
