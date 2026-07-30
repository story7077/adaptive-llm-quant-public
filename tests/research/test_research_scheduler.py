from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from trading.domain.hashing import canonical_hash
from trading.research.config import ResearchScheduleConfig, load_research_config
from trading.research.scheduler import (
    ResearchDispatchTarget,
    ResearchEvidenceMarker,
    ResearchScheduleWorkKind,
    VersionedResearchMarketSession,
    build_due_schedule_plans,
    build_operator_deep_research_plan,
    dispatch_target_for,
)


def _schedule() -> ResearchScheduleConfig:
    config_dir = Path(__file__).resolve().parents[2] / "config"
    return load_research_config(config_dir).config.schedule


def _session(
    *,
    identity: str,
    session_date: date,
    open_at: datetime,
    close_at: datetime,
    available_at: datetime | None = None,
) -> VersionedResearchMarketSession:
    return VersionedResearchMarketSession(
        calendar_session_id=identity,
        calendar_version="alpaca_market_calendar_v1",
        session_date=session_date,
        open_at=open_at,
        close_at=close_at,
        available_at=available_at or open_at - timedelta(days=1),
        session_hash=canonical_hash(
            {
                "identity": identity,
                "open_at": open_at,
                "close_at": close_at,
            }
        ),
    )


def _plans(
    *,
    as_of: datetime,
    sessions: tuple[VersionedResearchMarketSession, ...] = (),
    evidence: tuple[ResearchEvidenceMarker, ...] = (),
    consumed: frozenset[str] = frozenset(),
    include_outcome_maintenance: bool = False,
):
    return build_due_schedule_plans(
        schedule=_schedule(),
        config_manifest_hash="f" * 64,
        as_of=as_of,
        market_sessions=sessions,
        evidence=evidence,
        consumed_evidence_hashes=consumed,
        include_outcome_maintenance=include_outcome_maintenance,
    )


def test_daily_aggregation_uses_actual_early_close_and_holidays_have_no_work() -> None:
    early_close = _session(
        identity="early-close",
        session_date=date(2026, 11, 27),
        open_at=datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
        close_at=datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
    )
    plans = _plans(
        as_of=datetime(2026, 11, 28, 4, 0, tzinfo=UTC),
        sessions=(early_close,),
    )
    daily = tuple(
        item
        for item in plans
        if item.work_kind is ResearchScheduleWorkKind.DAILY_AGGREGATION
    )

    assert len(daily) == 1
    assert daily[0].calendar_session_id == "early-close"
    assert daily[0].scheduled_for == datetime(
        2026,
        11,
        27,
        23,
        0,
        tzinfo=UTC,
    )
    assert daily[0].scheduled_for > early_close.close_at
    holiday_plans = _plans(
        as_of=datetime(2026, 11, 27, 23, 0, tzinfo=UTC),
        sessions=(),
    )
    assert not any(
        item.work_kind is ResearchScheduleWorkKind.DAILY_AGGREGATION
        for item in holiday_plans
    )


def test_weekly_schedule_is_dst_safe_and_binds_latest_completed_session() -> None:
    before_dst = _session(
        identity="friday-before-dst",
        session_date=date(2026, 3, 6),
        open_at=datetime(2026, 3, 6, 14, 30, tzinfo=UTC),
        close_at=datetime(2026, 3, 6, 21, 0, tzinfo=UTC),
    )
    after_dst = _session(
        identity="friday-after-dst",
        session_date=date(2026, 3, 13),
        open_at=datetime(2026, 3, 13, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 3, 13, 20, 0, tzinfo=UTC),
    )
    plans = _plans(
        as_of=datetime(2026, 3, 15, 0, 0, tzinfo=UTC),
        sessions=(before_dst, after_dst),
    )
    weekly = tuple(
        item
        for item in plans
        if item.work_kind is ResearchScheduleWorkKind.WEEKLY_DEEP_RESEARCH
    )

    assert tuple(item.scheduled_for for item in weekly) == (
        datetime(2026, 3, 7, 15, 0, tzinfo=UTC),
        datetime(2026, 3, 14, 14, 0, tzinfo=UTC),
    )
    assert tuple(item.calendar_session_id for item in weekly) == (
        "friday-before-dst",
        "friday-after-dst",
    )


def test_evidence_trigger_requires_unique_unconsumed_threshold() -> None:
    start = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    evidence = tuple(
        ResearchEvidenceMarker(
            source_id=f"source-{index}",
            content_hash=canonical_hash({"source": index}),
            first_available_at=start + timedelta(minutes=index),
            captured_at=start + timedelta(minutes=index),
        )
        for index in range(8)
    )

    below = _plans(
        as_of=start + timedelta(minutes=7),
        evidence=evidence[:7],
    )
    assert not any(
        item.work_kind
        is ResearchScheduleWorkKind.EVIDENCE_TRIGGERED_RESEARCH
        for item in below
    )

    threshold = _plans(
        as_of=start + timedelta(minutes=8),
        evidence=(
            *evidence,
            evidence[0].model_copy(update={"source_id": "duplicate-content"}),
        ),
    )
    triggered = tuple(
        item
        for item in threshold
        if item.work_kind
        is ResearchScheduleWorkKind.EVIDENCE_TRIGGERED_RESEARCH
    )
    assert len(triggered) == 1
    assert len(triggered[0].trigger_content_hashes) == 8
    replay = _plans(
        as_of=start + timedelta(minutes=8),
        evidence=evidence,
        consumed=frozenset(triggered[0].trigger_content_hashes),
    )
    assert not any(
        item.work_kind
        is ResearchScheduleWorkKind.EVIDENCE_TRIGGERED_RESEARCH
        for item in replay
    )


def test_later_calendar_revision_does_not_change_an_already_bound_plan_identity() -> None:
    session_date = date(2026, 7, 24)
    first = _session(
        identity="calendar-revision-1",
        session_date=session_date,
        open_at=datetime(2026, 7, 24, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 7, 24, 20, 0, tzinfo=UTC),
    )
    late = _session(
        identity="calendar-revision-2",
        session_date=session_date,
        open_at=first.open_at,
        close_at=first.close_at,
        available_at=datetime(2026, 7, 26, 0, 0, tzinfo=UTC),
    )
    cutoff = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)

    before = _plans(as_of=cutoff, sessions=(first,))
    with_late_record = _plans(as_of=cutoff, sessions=(first, late))

    assert tuple(item.plan_hash for item in before) == tuple(
        item.plan_hash for item in with_late_record
    )


def test_recursive_maintenance_plans_follow_daily_aggregation_order() -> None:
    session = _session(
        identity="maintenance-session",
        session_date=date(2026, 7, 27),
        open_at=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 7, 27, 20, 0, tzinfo=UTC),
    )
    plans = _plans(
        as_of=datetime(2026, 7, 28, 0, 0, tzinfo=UTC),
        sessions=(session,),
        include_outcome_maintenance=True,
    )
    daily_chain = tuple(
        item.work_kind
        for item in plans
        if item.calendar_session_id == session.calendar_session_id
        and item.work_kind
        in {
            ResearchScheduleWorkKind.DAILY_AGGREGATION,
            ResearchScheduleWorkKind.OUTCOME_MATURATION,
            ResearchScheduleWorkKind.RESEARCH_MEMORY_MATERIALIZATION,
        }
    )
    assert daily_chain == (
        ResearchScheduleWorkKind.DAILY_AGGREGATION,
        ResearchScheduleWorkKind.OUTCOME_MATURATION,
        ResearchScheduleWorkKind.RESEARCH_MEMORY_MATERIALIZATION,
    )


def test_operator_deep_research_is_deterministic_and_completed_session_bound() -> None:
    session = _session(
        identity="operator-session",
        session_date=date(2026, 7, 30),
        open_at=datetime(2026, 7, 30, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 7, 30, 20, 0, tzinfo=UTC),
    )
    inputs = {
        "schedule": _schedule(),
        "config_manifest_hash": "f" * 64,
        "operator_trigger_id": "q1-v12-2026-07-30-post-session",
        "operator_reason_code": "FIRST_LIVE_SESSION",
        "scheduled_for": datetime(2026, 7, 30, 22, 5, tzinfo=UTC),
        "data_available_cutoff": datetime(
            2026,
            7,
            30,
            22,
            0,
            tzinfo=UTC,
        ),
        "session": session,
    }

    first = build_operator_deep_research_plan(**inputs)
    replay = build_operator_deep_research_plan(**inputs)

    assert first == replay
    assert first.work_kind is ResearchScheduleWorkKind.OPERATOR_DEEP_RESEARCH
    assert first.calendar_session_id == session.calendar_session_id
    assert first.calendar_session_hash == session.session_hash
    assert first.operator_trigger_id == inputs["operator_trigger_id"]
    assert first.operator_reason_code == inputs["operator_reason_code"]
    assert not first.trigger_source_ids
    assert (
        dispatch_target_for(first.work_kind)
        is ResearchDispatchTarget.DEEP_RESEARCH_CYCLE_V1
    )
    assert first.real_order_routing is False


def test_operator_deep_research_rejects_partial_or_unavailable_session() -> None:
    session = _session(
        identity="operator-incomplete-session",
        session_date=date(2026, 7, 30),
        open_at=datetime(2026, 7, 30, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 7, 30, 20, 0, tzinfo=UTC),
        available_at=datetime(2026, 7, 29, 20, 5, tzinfo=UTC),
    )
    common = {
        "schedule": _schedule(),
        "config_manifest_hash": "f" * 64,
        "operator_trigger_id": "q1-v12-incomplete-session",
        "operator_reason_code": "FIRST_LIVE_SESSION",
        "scheduled_for": datetime(2026, 7, 30, 22, 5, tzinfo=UTC),
        "session": session,
    }

    with pytest.raises(ValueError, match="completed market session"):
        build_operator_deep_research_plan(
            **common,
            data_available_cutoff=datetime(
                2026,
                7,
                30,
                19,
                59,
                tzinfo=UTC,
            ),
        )
    with pytest.raises(ValueError, match="unavailable at its cutoff"):
        build_operator_deep_research_plan(
            **{
                **common,
                "session": session.model_copy(
                    update={
                        "available_at": datetime(
                            2026,
                            7,
                            30,
                            22,
                            1,
                            tzinfo=UTC,
                        )
                    }
                ),
            },
            data_available_cutoff=datetime(
                2026,
                7,
                30,
                22,
                0,
                tzinfo=UTC,
            ),
        )
