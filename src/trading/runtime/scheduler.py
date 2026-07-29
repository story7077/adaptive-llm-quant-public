from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca_reference import MarketSession
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.persistence.models import PaperCycleRow, PaperRuntimeStatusRow
from trading.settings import ConfigBundle

NEW_YORK = ZoneInfo("America/New_York")
CYCLE_PRIORITY = {
    "BOOTSTRAP": 0,
    "Q1_SETTLEMENT": 0,
    "Q1_BOOTSTRAP": 1,
    "NAV": 1,
    "Q1_NAV_RISK": 2,
    "NEWS": 2,
    "DECISION": 3,
    "Q1_STRATEGIC": 3,
    "Q1_LLM_REVIEW": 4,
    "EXECUTION": 4,
    "Q1_EXECUTION": 5,
    "DAILY_REPORT": 5,
    "Q1_DAILY_RESULT": 6,
    "RECONCILIATION": 6,
}
RETRYABLE_KINDS = frozenset(
    {"BOOTSTRAP", "Q1_BOOTSTRAP", "Q1_DAILY_RESULT"}
)
KIND_GRACE = {
    "NAV": timedelta(minutes=20),
    "NEWS": timedelta(minutes=90),
    "DECISION": timedelta(minutes=90),
    "EXECUTION": timedelta(minutes=5),
    "DAILY_REPORT": timedelta(hours=4),
    "RECONCILIATION": timedelta(hours=4),
}


@dataclass(frozen=True, slots=True)
class PaperCycleSlot:
    cycle_kind: str
    scheduled_at: datetime


class PaperCycleFenceError(RuntimeError):
    """Raised when a stale worker attempts to mutate a reclaimed cycle."""


def build_session_slots(
    session: MarketSession,
    *,
    config: ConfigBundle,
) -> tuple[PaperCycleSlot, ...]:
    document = config.get("schedules.yaml")
    nav_minutes = _positive_int(document, "nav_snapshot_minutes")
    news_minutes = _positive_int(document, "news_poll_minutes")
    execution_minutes = _positive_int(
        config.get("forward-paper.yaml")["execution"],
        "poll_minutes",
    )
    slots: list[PaperCycleSlot] = [
        PaperCycleSlot("BOOTSTRAP", session.open_at),
    ]

    instant = session.open_at
    while instant <= session.close_at:
        slots.append(PaperCycleSlot("NAV", instant))
        instant += timedelta(minutes=nav_minutes)

    instant = session.open_at
    while instant <= session.close_at:
        slots.append(PaperCycleSlot("NEWS", instant))
        instant += timedelta(minutes=news_minutes)

    instant = session.open_at
    while instant <= session.close_at:
        slots.append(PaperCycleSlot("EXECUTION", instant))
        instant += timedelta(minutes=execution_minutes)

    for raw in document.get("decision_times_et", ()):
        decision = _session_clock(session.session_date, str(raw))
        if session.open_at <= decision <= session.close_at:
            slots.append(PaperCycleSlot("DECISION", decision))

    report_time = _session_clock(
        session.session_date,
        str(document["daily_report_time_et"]),
    )
    reconciliation_time = _session_clock(
        session.session_date,
        str(document["daily_reconciliation_time_et"]),
    )
    slots.extend(
        (
            PaperCycleSlot("DAILY_REPORT", report_time),
            PaperCycleSlot("RECONCILIATION", reconciliation_time),
        )
    )
    return tuple(
        sorted(
            set(slots),
            key=lambda item: (
                item.scheduled_at,
                CYCLE_PRIORITY.get(item.cycle_kind, 99),
                item.cycle_kind,
            ),
        )
    )


class PaperCycleStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def ensure_slots(
        self,
        *,
        run_id: str,
        slots: tuple[PaperCycleSlot, ...],
        now: datetime,
    ) -> int:
        instant = require_aware_utc(now)
        created = 0
        with self._session_factory.begin() as session:
            for slot in slots:
                scheduled_at = require_aware_utc(slot.scheduled_at)
                cycle_id = stable_id(
                    "paper-cycle",
                    run_id,
                    slot.cycle_kind,
                    scheduled_at,
                )
                if session.get(PaperCycleRow, cycle_id) is not None:
                    continue
                session.add(
                    PaperCycleRow(
                        cycle_id=cycle_id,
                        run_id=run_id,
                        cycle_kind=slot.cycle_kind,
                        scheduled_at=scheduled_at,
                        data_available_cutoff=None,
                        status="PENDING",
                        idempotency_key=(
                            f"{run_id}:{slot.cycle_kind}:"
                            f"{scheduled_at.isoformat()}"
                        ),
                        lease_owner=None,
                        lease_expires_at=None,
                        attempt_count=0,
                        input_manifest_hash=None,
                        output_manifest_hash=None,
                        started_at=None,
                        completed_at=None,
                        last_error_code=None,
                        last_error_detail=None,
                        created_at=instant,
                        updated_at=instant,
                    )
                )
                created += 1
        return created

    def claim_next(
        self,
        *,
        run_id: str,
        now: datetime,
        grace: timedelta,
        lease: timedelta = timedelta(minutes=45),
        owner: str | None = None,
        kinds: frozenset[str] | None = None,
    ) -> PaperCycleRow | None:
        requested_now = require_aware_utc(now)
        lease_owner = owner or _lease_owner()
        while True:
            with self._session_factory.begin() as session:
                # PostgreSQL is the lease authority. A host clock that is ahead
                # must not make a cycle due early, reclaim another worker's
                # lease early, or timestamp a claim in the future. SQLite keeps
                # the caller-supplied clock so deterministic tests and replays
                # remain injectable.
                instant = _lease_check_now(session, requested_now)
                predicates = [
                    PaperCycleRow.run_id == run_id,
                    PaperCycleRow.scheduled_at <= instant,
                    or_(
                        PaperCycleRow.status == "PENDING",
                        (
                            (PaperCycleRow.status == "RUNNING")
                            & (PaperCycleRow.lease_expires_at <= instant)
                        ),
                    ),
                ]
                if kinds is not None:
                    predicates.append(PaperCycleRow.cycle_kind.in_(tuple(kinds)))
                statement = (
                    select(PaperCycleRow)
                    .where(*predicates)
                    .order_by(
                        PaperCycleRow.scheduled_at,
                        case(
                            CYCLE_PRIORITY,
                            value=PaperCycleRow.cycle_kind,
                            else_=99,
                        ),
                        PaperCycleRow.cycle_kind,
                    )
                    .limit(1)
                )
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    statement = statement.with_for_update(skip_locked=True)
                row = session.scalar(statement)
                if row is None:
                    return None
                if (
                    row.cycle_kind not in RETRYABLE_KINDS
                    and instant
                    > _aware(row.scheduled_at)
                    + max(grace, KIND_GRACE.get(row.cycle_kind, grace))
                ):
                    row.status = "SKIPPED_MISSED_WINDOW"
                    row.completed_at = instant
                    row.updated_at = instant
                    row.last_error_code = "MISSED_WINDOW"
                    row.last_error_detail = "Cycle was not claimed within its grace window"
                    continue
                row.status = "RUNNING"
                row.lease_owner = lease_owner
                row.lease_expires_at = instant + lease
                row.attempt_count += 1
                row.started_at = row.started_at or instant
                row.updated_at = instant
                session.flush()
                session.expunge(row)
                return row

    def complete(
        self,
        cycle_id: str,
        *,
        lease_owner: str,
        attempt_count: int,
        cutoff: datetime,
        input_manifest: object,
        output_manifest: object,
        now: datetime,
    ) -> None:
        instant = require_aware_utc(now)
        input_hash = canonical_hash(input_manifest)
        output_hash = canonical_hash(output_manifest)
        with self._session_factory.begin() as session:
            row = _locked_cycle(session, cycle_id)
            if row is None:
                raise ValueError(f"Unknown paper cycle: {cycle_id}")
            if row.status == "COMPLETED":
                if (
                    row.input_manifest_hash != input_hash
                    or row.output_manifest_hash != output_hash
                ):
                    raise PaperCycleFenceError(
                        "Completed cycle was replayed with different manifests"
                    )
                return
            _require_cycle_lease(
                row,
                owner=lease_owner,
                attempt_count=attempt_count,
                now=_lease_check_now(session, instant),
            )
            row.data_available_cutoff = require_aware_utc(cutoff)
            row.input_manifest_hash = input_hash
            row.output_manifest_hash = output_hash
            row.status = "COMPLETED"
            row.completed_at = instant
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_error_code = None
            row.last_error_detail = None
            row.updated_at = instant

    def defer(
        self,
        cycle_id: str,
        *,
        lease_owner: str,
        attempt_count: int,
        code: str,
        detail: str,
        now: datetime,
    ) -> None:
        instant = require_aware_utc(now)
        with self._session_factory.begin() as session:
            row = _locked_cycle(session, cycle_id)
            if row is None:
                raise ValueError(f"Unknown paper cycle: {cycle_id}")
            _require_cycle_lease(
                row,
                owner=lease_owner,
                attempt_count=attempt_count,
                now=_lease_check_now(session, instant),
            )
            row.status = "PENDING"
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_error_code = code[:80]
            row.last_error_detail = detail[:500]
            row.updated_at = instant

    def fail(
        self,
        cycle_id: str,
        *,
        lease_owner: str,
        attempt_count: int,
        code: str,
        detail: str,
        now: datetime,
    ) -> None:
        instant = require_aware_utc(now)
        with self._session_factory.begin() as session:
            row = _locked_cycle(session, cycle_id)
            if row is None:
                raise ValueError(f"Unknown paper cycle: {cycle_id}")
            _require_cycle_lease(
                row,
                owner=lease_owner,
                attempt_count=attempt_count,
                now=_lease_check_now(session, instant),
            )
            row.status = "FAILED"
            row.completed_at = instant
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_error_code = code[:80]
            row.last_error_detail = detail[:500]
            row.updated_at = instant

    def heartbeat(
        self,
        *,
        run_id: str,
        state: str,
        now: datetime,
        current_cycle_id: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        instant = require_aware_utc(now)
        with self._session_factory.begin() as session:
            row = session.get(PaperRuntimeStatusRow, run_id)
            if row is None:
                row = PaperRuntimeStatusRow(
                    run_id=run_id,
                    state=state,
                    current_cycle_id=current_cycle_id,
                    heartbeat_at=instant,
                    last_completed_cycle_at=None,
                    last_error_code=error_code,
                    last_error_detail=(
                        None if error_detail is None else error_detail[:500]
                    ),
                    process_id=os.getpid(),
                    updated_at=instant,
                )
                session.add(row)
                return
            row.state = state
            row.current_cycle_id = current_cycle_id
            row.heartbeat_at = instant
            row.last_error_code = error_code
            row.last_error_detail = (
                None if error_detail is None else error_detail[:500]
            )
            row.process_id = os.getpid()
            row.updated_at = instant
            if current_cycle_id is None:
                row.last_completed_cycle_at = instant

    def status(self, run_id: str, *, cycle_limit: int = 30) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            runtime = session.get(PaperRuntimeStatusRow, run_id)
            cycles = list(
                session.scalars(
                    select(PaperCycleRow)
                    .where(
                        PaperCycleRow.run_id == run_id,
                        PaperCycleRow.scheduled_at <= now,
                    )
                    .order_by(PaperCycleRow.scheduled_at.desc())
                    .limit(cycle_limit)
                )
            )
            next_cycle = session.scalar(
                select(PaperCycleRow)
                .where(
                    PaperCycleRow.run_id == run_id,
                    PaperCycleRow.status == "PENDING",
                    PaperCycleRow.scheduled_at > now,
                )
                .order_by(
                    PaperCycleRow.scheduled_at,
                    case(
                        CYCLE_PRIORITY,
                        value=PaperCycleRow.cycle_kind,
                        else_=99,
                    ),
                    PaperCycleRow.cycle_kind,
                )
                .limit(1)
            )
        return {
            "runtime": (
                None
                if runtime is None
                else {
                    "state": runtime.state,
                    "heartbeat_at": _iso_or_none(runtime.heartbeat_at),
                    "current_cycle_id": runtime.current_cycle_id,
                    "last_error_code": runtime.last_error_code,
                    "last_error_detail": runtime.last_error_detail,
                    "process_id": runtime.process_id,
                }
            ),
            "cycles": [
                {
                    "cycle_id": row.cycle_id,
                    "kind": row.cycle_kind,
                    "scheduled_at": _iso_or_none(row.scheduled_at),
                    "status": row.status,
                    "attempt_count": row.attempt_count,
                    "completed_at": _iso_or_none(row.completed_at),
                    "last_error_code": row.last_error_code,
                    "last_error_detail": row.last_error_detail,
                }
                for row in cycles
            ],
            "next_cycle": (
                None
                if next_cycle is None
                else {
                    "cycle_id": next_cycle.cycle_id,
                    "kind": next_cycle.cycle_kind,
                    "scheduled_at": _iso_or_none(next_cycle.scheduled_at),
                    "status": next_cycle.status,
                }
            ),
        }


def _session_clock(session_date: date, raw: str) -> datetime:
    parsed = time.fromisoformat(raw)
    return datetime.combine(session_date, parsed, tzinfo=NEW_YORK).astimezone(UTC)


def _positive_int(document: dict[str, Any], key: str) -> int:
    value = int(document[key])
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def _lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _locked_cycle(session: Session, cycle_id: str) -> PaperCycleRow | None:
    statement = select(PaperCycleRow).where(PaperCycleRow.cycle_id == cycle_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    return session.scalar(statement)


def _lease_check_now(session: Session, fallback: datetime) -> datetime:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return fallback
    database_now = session.scalar(select(func.clock_timestamp()))
    if database_now is None:
        raise PaperCycleFenceError("Database clock is unavailable")
    return _aware(database_now)


def _require_cycle_lease(
    row: PaperCycleRow,
    *,
    owner: str,
    attempt_count: int,
    now: datetime,
) -> None:
    if (
        row.status != "RUNNING"
        or row.lease_owner != owner
        or row.attempt_count != attempt_count
        or row.lease_expires_at is None
        or _aware(row.lease_expires_at) <= now
    ):
        raise PaperCycleFenceError(
            f"Cycle lease is no longer owned by {owner} attempt {attempt_count}"
        )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else _aware(value).isoformat().replace("+00:00", "Z")
