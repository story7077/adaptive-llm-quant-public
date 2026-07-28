from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from trading.data.alpaca_reference import MarketSession
from trading.domain.hashing import stable_id
from trading.runtime.scheduler import PaperCycleSlot

NEW_YORK = ZoneInfo("America/New_York")
Q1_CYCLE_KINDS = frozenset(
    {
        "Q1_SETTLEMENT",
        "Q1_BOOTSTRAP",
        "Q1_NAV_RISK",
        "Q1_STRATEGIC",
        "Q1_LLM_REVIEW",
        "Q1_EXECUTION",
        "Q1_DAILY_RESULT",
    }
)
Q1_CYCLE_PRIORITY = (
    "Q1_SETTLEMENT",
    "Q1_BOOTSTRAP",
    "Q1_NAV_RISK",
    "Q1_STRATEGIC",
    "Q1_LLM_REVIEW",
    "Q1_EXECUTION",
    "Q1_DAILY_RESULT",
)


@dataclass(frozen=True, slots=True)
class Q1SessionSchedule:
    first_nav_time_et: time
    nav_interval_minutes: int
    strategic_time_et: time
    llm_review_times_et: tuple[time, ...]
    normal_execution_start_et: time
    normal_execution_end_et: time
    execution_interval_minutes: int
    no_risk_increase_after_et: time

    def __post_init__(self) -> None:
        if self.nav_interval_minutes <= 0 or self.execution_interval_minutes <= 0:
            raise ValueError("Q1 schedule intervals must be positive")
        if self.normal_execution_end_et < self.normal_execution_start_et:
            raise ValueError("Q1 normal execution window is inverted")

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> Q1SessionSchedule:
        schedule = _mapping(document, "schedule")
        return cls(
            first_nav_time_et=_clock(schedule, "first_nav_time_et"),
            nav_interval_minutes=_positive_int(schedule, "nav_interval_minutes"),
            strategic_time_et=_clock(schedule, "strategic_time_et"),
            llm_review_times_et=tuple(
                _parse_clock(str(value))
                for value in _sequence(schedule, "llm_review_times_et")
            ),
            normal_execution_start_et=_clock(
                schedule,
                "normal_execution_start_et",
            ),
            normal_execution_end_et=_clock(
                schedule,
                "normal_execution_end_et",
            ),
            execution_interval_minutes=_positive_int(
                schedule,
                "execution_interval_minutes",
            ),
            no_risk_increase_after_et=_clock(
                schedule,
                "no_risk_increase_after_et",
            ),
        )


@dataclass(frozen=True, slots=True)
class VersionedMarketSession:
    calendar_session_id: str
    calendar_version: str
    session_date: date
    open_at: datetime
    close_at: datetime
    source_payload_hash: str
    source_available_at: datetime

    @classmethod
    def from_reference(
        cls,
        session: MarketSession,
        *,
        calendar_version: str,
    ) -> VersionedMarketSession:
        return cls(
            calendar_session_id=stable_id(
                "market-calendar-session",
                calendar_version,
                session.session_date,
                session.open_at,
                session.close_at,
                session.payload_hash,
            ),
            calendar_version=calendar_version,
            session_date=session.session_date,
            open_at=_aware(session.open_at),
            close_at=_aware(session.close_at),
            source_payload_hash=session.payload_hash,
            source_available_at=_aware(session.available_at),
        )


def build_q1_session_slots(
    session: VersionedMarketSession,
    *,
    schedule: Q1SessionSchedule,
) -> tuple[PaperCycleSlot, ...]:
    """Build one session strictly from its versioned open and close."""

    if session.close_at <= session.open_at:
        raise ValueError("Market session close must follow open")
    slots: set[PaperCycleSlot] = {
        PaperCycleSlot("Q1_SETTLEMENT", session.open_at),
        PaperCycleSlot("Q1_BOOTSTRAP", session.open_at),
        PaperCycleSlot("Q1_DAILY_RESULT", session.close_at),
    }

    first_nav = _session_clock(
        session.session_date,
        schedule.first_nav_time_et,
    )
    nav_at = max(session.open_at, first_nav)
    while nav_at < session.close_at:
        slots.add(PaperCycleSlot("Q1_NAV_RISK", nav_at))
        nav_at += timedelta(minutes=schedule.nav_interval_minutes)

    strategic_at = _session_clock(
        session.session_date,
        schedule.strategic_time_et,
    )
    if session.open_at <= strategic_at < session.close_at:
        slots.add(PaperCycleSlot("Q1_STRATEGIC", strategic_at))

    for review_clock in schedule.llm_review_times_et:
        review_at = _session_clock(session.session_date, review_clock)
        if (
            review_clock != schedule.strategic_time_et
            and session.open_at <= review_at < session.close_at
        ):
            slots.add(PaperCycleSlot("Q1_LLM_REVIEW", review_at))

    execution_at = session.open_at + timedelta(
        minutes=schedule.execution_interval_minutes
    )
    while execution_at < session.close_at:
        slots.add(PaperCycleSlot("Q1_EXECUTION", execution_at))
        execution_at += timedelta(minutes=schedule.execution_interval_minutes)

    return tuple(
        sorted(
            slots,
            key=lambda item: (
                item.scheduled_at,
                _cycle_priority(item.cycle_kind),
                item.cycle_kind,
            ),
        )
    )


def normal_order_window(
    session: VersionedMarketSession,
    *,
    schedule: Q1SessionSchedule,
) -> tuple[datetime, datetime]:
    start = max(
        session.open_at,
        _session_clock(session.session_date, schedule.normal_execution_start_et),
    )
    configured_end = _session_clock(
        session.session_date,
        schedule.normal_execution_end_et,
    )
    return start, min(configured_end, session.close_at)


def normal_order_valid_until(
    session: VersionedMarketSession,
    *,
    schedule: Q1SessionSchedule,
) -> datetime:
    return normal_order_window(session, schedule=schedule)[1]


def risk_increase_allowed(
    instant: datetime,
    session: VersionedMarketSession,
    *,
    schedule: Q1SessionSchedule,
) -> bool:
    cutoff = _session_clock(
        session.session_date,
        schedule.no_risk_increase_after_et,
    )
    value = _aware(instant)
    return session.open_at <= value < min(cutoff, session.close_at)


def is_regular_session_time(
    instant: datetime,
    session: VersionedMarketSession,
) -> bool:
    value = _aware(instant)
    return session.open_at <= value < session.close_at


def _cycle_priority(kind: str) -> int:
    try:
        return Q1_CYCLE_PRIORITY.index(kind)
    except ValueError:
        return len(Q1_CYCLE_PRIORITY)


def _session_clock(session_date: date, value: time) -> datetime:
    return datetime.combine(session_date, value, tzinfo=NEW_YORK).astimezone(UTC)


def _clock(document: dict[str, Any], key: str) -> time:
    return _parse_clock(str(document[key]))


def _parse_clock(raw: str) -> time:
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"Invalid ET clock {raw!r}; expected HH:MM") from exc


def _mapping(document: dict[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Q1 config {key!r} must be an object")
    return cast(dict[str, Any], value)


def _sequence(document: dict[str, Any], key: str) -> tuple[object, ...]:
    value = document.get(key)
    if not isinstance(value, list | tuple):
        raise ValueError(f"Q1 config {key!r} must be a sequence")
    return tuple(cast(list[object] | tuple[object, ...], value))


def _positive_int(document: dict[str, Any], key: str) -> int:
    value = int(document[key])
    if value <= 0:
        raise ValueError(f"Q1 config {key!r} must be positive")
    return value


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
