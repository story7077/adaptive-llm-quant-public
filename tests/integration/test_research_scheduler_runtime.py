from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from trading.cli import app
from trading.domain.hashing import canonical_hash, stable_id
from trading.persistence.models import (
    FillRow,
    MarketCalendarSessionRow,
    OrderIntentRow,
    PaperCycleRow,
)
from trading.persistence.research_scheduler import (
    ResearchScheduleEventRow,
    ResearchScheduleFenceError,
    ResearchSchedulerRepository,
    ResearchScheduleWorkItemRow,
)
from trading.research.config import load_research_config
from trading.research.dispatch_execution import (
    ResearchExecutionArtifactKind,
    ResearchExecutionArtifactV1,
    ResearchWorkExecutionResultV1,
    build_execution_request,
    build_execution_result,
)
from trading.research.scheduler import (
    ResearchScheduleEventType,
    ResearchScheduleWorkKind,
    ResearchWorkExecutionLeaseV1,
    ResearchWorkLeaseV1,
    build_due_schedule_plans,
)
from trading.runtime.research_scheduler import ResearchSchedulerService

PAST_SESSION_DATE = date(2020, 7, 24)
PAST_OPEN = datetime(2020, 7, 24, 13, 30, tzinfo=UTC)
PAST_CLOSE = datetime(2020, 7, 24, 20, 0, tzinfo=UTC)
PLAN_AS_OF = datetime(2020, 7, 25, 2, 0, tzinfo=UTC)


def _config(repository_root: Path):
    return load_research_config(repository_root / "config")


def _seed_calendar(factory) -> None:
    payload = {
        "calendar_session_id": "calendar-2020-07-24",
        "calendar_version": "alpaca_market_calendar_v1",
        "session_date": PAST_SESSION_DATE.isoformat(),
        "open_at": PAST_OPEN.isoformat(),
        "close_at": PAST_CLOSE.isoformat(),
        "available_at": (PAST_OPEN - timedelta(days=1)).isoformat(),
    }
    with factory.begin() as session:
        session.add(
            MarketCalendarSessionRow(
                calendar_session_id="calendar-2020-07-24",
                algorithm_version="q1_math_core_v1",
                calendar_version="alpaca_market_calendar_v1",
                session_date=PAST_SESSION_DATE,
                open_at=PAST_OPEN,
                close_at=PAST_CLOSE,
                source="SYNTHETIC_TEST",
                available_at=PAST_OPEN - timedelta(days=1),
                config_manifest_hash="a" * 64,
                code_version="test-v1",
                model_version="none",
                source_manifest_hash="b" * 64,
                session_hash=canonical_hash(payload),
                payload_json=payload,
                created_at=PAST_OPEN - timedelta(days=1),
            )
        )


def _service(factory, repository_root: Path):
    repository = ResearchSchedulerRepository(factory)
    return (
        repository,
        ResearchSchedulerService(
            repository=repository,
            config=_config(repository_root),
        ),
    )


def _claim_execution(
    repository: ResearchSchedulerRepository,
    *,
    consumer_id: str,
) -> ResearchWorkExecutionLeaseV1:
    lease = repository.claim_execution(
        consumer_id=consumer_id,
        lease_seconds=900,
    )
    assert lease is not None
    return lease


def _maintenance_result(
    repository: ResearchSchedulerRepository,
    lease: ResearchWorkExecutionLeaseV1,
) -> ResearchWorkExecutionResultV1:
    plan, receipt = repository.execution_input(execution_lease=lease)
    request = build_execution_request(
        execution_lease=lease,
        plan=plan,
        receipt=receipt,
        commander_selection=None,
    )
    artifact_kind = {
        ResearchScheduleWorkKind.DAILY_AGGREGATION: (
            ResearchExecutionArtifactKind.DAILY_AGGREGATION
        ),
        ResearchScheduleWorkKind.OUTCOME_MATURATION: (
            ResearchExecutionArtifactKind.OUTCOME_MATURATION
        ),
        ResearchScheduleWorkKind.RESEARCH_MEMORY_MATERIALIZATION: (
            ResearchExecutionArtifactKind.RESEARCH_MEMORY_SNAPSHOT
        ),
    }[lease.work_kind]
    return build_execution_result(
        request=request,
        artifacts=(
            ResearchExecutionArtifactV1(
                artifact_kind=artifact_kind,
                content_hash=canonical_hash(
                    {
                        "execution_id": lease.execution_id,
                        "artifact_kind": artifact_kind,
                    }
                ),
                record_count=1,
            ),
        ),
        invocations=(),
        decision_kind=None,
        completed_at=lease.acquired_at,
    )


def test_schedule_plan_and_dispatch_are_idempotent(
    sqlite_database,
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    _seed_calendar(factory)
    repository, service = _service(factory, repository_root)

    first = service.plan(as_of=PLAN_AS_OF)
    second = service.plan(as_of=PLAN_AS_OF)
    assert first["created_count"] == 1
    assert second["created_count"] == 0
    assert first["plan_hashes"] == second["plan_hashes"]

    dispatched = service.dispatch_once(worker_id="scheduler-worker-1")
    assert dispatched["dispatched"] is True
    receipt = dispatched["receipt"]
    assert receipt["real_order_routing"] is False
    assert receipt["dispatch_target"] == "RESEARCH_DAILY_AGGREGATION_V1"
    assert service.dispatch_once(worker_id="scheduler-worker-1")[
        "reason_code"
    ] == "NO_DUE_RESEARCH_WORK"

    history = repository.event_history(str(receipt["work_item_id"]))
    assert tuple(item.event_type for item in history) == (
        ResearchScheduleEventType.PLANNED.value,
        ResearchScheduleEventType.LEASE_ACQUIRED.value,
        ResearchScheduleEventType.DISPATCHED.value,
    )
    execution_lease = _claim_execution(
        repository,
        consumer_id="research-consumer-1",
    )
    assert (
        repository.claim_execution(
            consumer_id="research-consumer-2",
            lease_seconds=900,
        )
        is None
    )
    execution_result = _maintenance_result(
        repository,
        execution_lease,
    )
    assert repository.record_execution_outcome(
        execution_lease=execution_lease,
        succeeded=True,
        reason_code=None,
        maximum_attempts=3,
        result=execution_result,
    )
    assert not repository.record_execution_outcome(
        execution_lease=execution_lease,
        succeeded=True,
        reason_code=None,
        maximum_attempts=3,
        result=execution_result,
    )


def test_failed_execution_is_append_only_and_retry_safe(
    sqlite_database,
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    _seed_calendar(factory)
    repository, service = _service(factory, repository_root)
    service.plan(as_of=PLAN_AS_OF)

    first = service.dispatch_once(worker_id="scheduler-worker-a")
    first_receipt = first["receipt"]
    first_execution_lease = _claim_execution(
        repository,
        consumer_id="research-consumer-a",
    )
    assert first_execution_lease.receipt_id == first_receipt["receipt_id"]
    assert repository.record_execution_outcome(
        execution_lease=first_execution_lease,
        succeeded=False,
        reason_code="SYNTHETIC_EXECUTION_FAILURE",
        maximum_attempts=3,
    )

    retry = service.dispatch_once(worker_id="scheduler-worker-b")
    retry_receipt = retry["receipt"]
    assert retry_receipt["attempt_number"] == 2
    with pytest.raises(ResearchScheduleFenceError, match="stale"):
        repository.record_execution_outcome(
            execution_lease=first_execution_lease,
            succeeded=True,
            reason_code=None,
            maximum_attempts=3,
            result=_maintenance_result(
                repository,
                first_execution_lease,
            ),
        )
    retry_execution_lease = _claim_execution(
        repository,
        consumer_id="research-consumer-b",
    )
    assert retry_execution_lease.receipt_id == retry_receipt["receipt_id"]
    assert repository.record_execution_outcome(
        execution_lease=retry_execution_lease,
        succeeded=True,
        reason_code=None,
        maximum_attempts=3,
        result=_maintenance_result(repository, retry_execution_lease),
    )
    history = repository.event_history(str(first_receipt["work_item_id"]))
    assert tuple(item.event_type for item in history) == (
        "PLANNED",
        "LEASE_ACQUIRED",
        "DISPATCHED",
        "EXECUTION_LEASE_ACQUIRED",
        "FAILED",
        "LEASE_ACQUIRED",
        "DISPATCHED",
        "EXECUTION_LEASE_ACQUIRED",
        "SUCCEEDED",
    )


def test_outcome_and_memory_work_wait_for_predecessor_success(
    sqlite_database,
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    _seed_calendar(factory)
    repository = ResearchSchedulerRepository(factory)
    config = _config(repository_root)
    sessions, evidence, consumed = repository.planning_inputs(
        as_of=PLAN_AS_OF,
        calendar_version=config.config.schedule.market_calendar_version,
    )
    plans = build_due_schedule_plans(
        schedule=config.config.schedule,
        config_manifest_hash=config.manifest_hash,
        as_of=PLAN_AS_OF,
        market_sessions=sessions,
        evidence=evidence,
        consumed_evidence_hashes=consumed,
        include_outcome_maintenance=True,
    )
    assert repository.store_plans(plans, created_at=PLAN_AS_OF) == 3

    daily_lease = repository.claim_next(
        lease_owner="daily-worker",
        lease_seconds=900,
        maximum_attempts=3,
    )
    assert daily_lease is not None
    assert daily_lease.work_kind is ResearchScheduleWorkKind.DAILY_AGGREGATION
    daily_receipt = repository.commit_dispatch(lease=daily_lease)
    assert (
        repository.claim_next(
            lease_owner="blocked-worker",
            lease_seconds=900,
            maximum_attempts=3,
        )
        is None
    )
    daily_execution_lease = _claim_execution(
        repository,
        consumer_id="daily-consumer",
    )
    assert daily_execution_lease.receipt_id == daily_receipt.receipt_id
    repository.record_execution_outcome(
        execution_lease=daily_execution_lease,
        succeeded=True,
        reason_code=None,
        maximum_attempts=3,
        result=_maintenance_result(repository, daily_execution_lease),
    )

    outcome_lease = repository.claim_next(
        lease_owner="outcome-worker",
        lease_seconds=900,
        maximum_attempts=3,
    )
    assert outcome_lease is not None
    assert outcome_lease.work_kind is ResearchScheduleWorkKind.OUTCOME_MATURATION
    outcome_receipt = repository.commit_dispatch(lease=outcome_lease)
    assert (
        repository.claim_next(
            lease_owner="still-blocked-worker",
            lease_seconds=900,
            maximum_attempts=3,
        )
        is None
    )
    outcome_execution_lease = _claim_execution(
        repository,
        consumer_id="outcome-consumer",
    )
    assert outcome_execution_lease.receipt_id == outcome_receipt.receipt_id
    repository.record_execution_outcome(
        execution_lease=outcome_execution_lease,
        succeeded=True,
        reason_code=None,
        maximum_attempts=3,
        result=_maintenance_result(repository, outcome_execution_lease),
    )

    memory_lease = repository.claim_next(
        lease_owner="memory-worker",
        lease_seconds=900,
        maximum_attempts=3,
    )
    assert memory_lease is not None
    assert (
        memory_lease.work_kind
        is ResearchScheduleWorkKind.RESEARCH_MEMORY_MATERIALIZATION
    )


def test_blocked_maintenance_backlog_cannot_starve_later_daily_work(
    sqlite_database,
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    _seed_calendar(factory)
    repository = ResearchSchedulerRepository(factory)
    config = _config(repository_root)
    sessions, evidence, consumed = repository.planning_inputs(
        as_of=PLAN_AS_OF,
        calendar_version=config.config.schedule.market_calendar_version,
    )
    daily_plan = build_due_schedule_plans(
        schedule=config.config.schedule,
        config_manifest_hash=config.manifest_hash,
        as_of=PLAN_AS_OF,
        market_sessions=sessions,
        evidence=evidence,
        consumed_evidence_hashes=consumed,
    )[0]
    assert repository.store_plans(
        (daily_plan,),
        created_at=PLAN_AS_OF,
    ) == 1

    with factory.begin() as session:
        pending_events: list[ResearchScheduleEventRow] = []
        for index in range(100):
            work_item_id = f"blocked-outcome-{index:03d}"
            plan_hash = canonical_hash(
                {"kind": "blocked-outcome", "index": index}
            )
            session.add(
                ResearchScheduleWorkItemRow(
                    work_item_id=work_item_id,
                    schema_version="research_schedule_plan_v1",
                    work_kind=(
                        ResearchScheduleWorkKind.OUTCOME_MATURATION.value
                    ),
                    idempotency_key=f"blocked-work-{index:03d}",
                    schedule_version=daily_plan.schedule_version,
                    scheduled_for=PAST_CLOSE,
                    data_available_cutoff=PAST_CLOSE,
                    calendar_session_id=daily_plan.calendar_session_id,
                    trigger_manifest_hash="c" * 64,
                    config_manifest_hash=daily_plan.config_manifest_hash,
                    plan_hash=plan_hash,
                    real_order_routing=False,
                    payload_json={"synthetic_blocked_row": index},
                    created_at=PAST_CLOSE,
                )
            )
            pending_events.append(
                ResearchScheduleEventRow(
                    event_id=f"blocked-event-{index:03d}",
                    work_item_id=work_item_id,
                    sequence=1,
                    event_type=ResearchScheduleEventType.PLANNED.value,
                    attempt_number=0,
                    retryable=True,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    receipt_id=None,
                    idempotency_key=f"blocked-event-key-{index:03d}",
                    event_hash=canonical_hash(
                        {"kind": "blocked-event", "index": index}
                    ),
                    config_manifest_hash=daily_plan.config_manifest_hash,
                    real_order_routing=False,
                    payload_json={"synthetic_blocked_event": index},
                    created_at=PAST_CLOSE,
                )
            )
        session.flush()
        session.add_all(pending_events)

    lease = repository.claim_next(
        lease_owner="non-starved-worker",
        lease_seconds=900,
        maximum_attempts=3,
    )

    assert lease is not None
    assert lease.work_item_id == daily_plan.work_item_id
    assert lease.work_kind is ResearchScheduleWorkKind.DAILY_AGGREGATION


def test_expired_lease_is_reclaimed_and_stale_worker_cannot_commit(
    sqlite_database,
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    _seed_calendar(factory)
    repository, service = _service(factory, repository_root)
    planning = service.plan(as_of=PLAN_AS_OF)
    work_item_id = str(planning["work_item_ids"][0])
    plan_hash = str(planning["plan_hashes"][0])
    config_hash = _config(repository_root).manifest_hash
    expired_at = datetime(2020, 7, 25, 3, 0, tzinfo=UTC)
    stale_lease = ResearchWorkLeaseV1(
        work_item_id=work_item_id,
        work_kind=ResearchScheduleWorkKind.DAILY_AGGREGATION,
        lease_owner="stale-worker",
        lease_token="stale-lease-token",
        attempt_number=1,
        acquired_at=expired_at - timedelta(minutes=1),
        lease_expires_at=expired_at,
        config_manifest_hash=config_hash,
        plan_hash=plan_hash,
        real_order_routing=False,
    )
    with factory.begin() as session:
        session.add(
            ResearchScheduleEventRow(
                event_id="synthetic-expired-lease",
                work_item_id=work_item_id,
                sequence=2,
                event_type="LEASE_ACQUIRED",
                attempt_number=1,
                retryable=True,
                lease_owner=stale_lease.lease_owner,
                lease_token=stale_lease.lease_token,
                lease_expires_at=stale_lease.lease_expires_at,
                receipt_id=None,
                idempotency_key="synthetic-expired-lease",
                event_hash="e" * 64,
                config_manifest_hash=config_hash,
                real_order_routing=False,
                payload_json={"reason": "synthetic expired lease"},
                created_at=stale_lease.acquired_at,
            )
        )

    reclaimed = repository.claim_next(
        lease_owner="replacement-worker",
        lease_seconds=900,
        maximum_attempts=3,
    )
    assert reclaimed is not None
    assert reclaimed.attempt_number == 2
    assert repository.event_history(work_item_id)[-1].event_type == (
        ResearchScheduleEventType.LEASE_RECLAIMED.value
    )
    with pytest.raises(ResearchScheduleFenceError, match="stale"):
        repository.commit_dispatch(lease=stale_lease)
    receipt = repository.commit_dispatch(lease=reclaimed)
    assert receipt.attempt_number == 2


def test_expired_execution_lease_is_reclaimed_and_stale_consumer_is_fenced(
    sqlite_database,
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    _seed_calendar(factory)
    repository, service = _service(factory, repository_root)
    service.plan(as_of=PLAN_AS_OF)
    dispatched = service.dispatch_once(worker_id="dispatch-worker")
    receipt = dispatched["receipt"]
    work_item_id = str(receipt["work_item_id"])
    receipt_id = str(receipt["receipt_id"])
    receipt_hash = str(receipt["receipt_hash"])
    config_hash = _config(repository_root).manifest_hash
    expired_at = datetime(2020, 7, 25, 3, 0, tzinfo=UTC)
    stale_lease = ResearchWorkExecutionLeaseV1(
        execution_id=stable_id(
            "research-work-execution",
            receipt_id,
        ),
        receipt_id=receipt_id,
        receipt_hash=receipt_hash,
        work_item_id=work_item_id,
        work_kind=ResearchScheduleWorkKind.DAILY_AGGREGATION,
        consumer_id="stale-consumer",
        execution_token="stale-execution-token",
        dispatch_attempt_number=1,
        acquired_at=expired_at - timedelta(minutes=1),
        lease_expires_at=expired_at,
        config_manifest_hash=config_hash,
        real_order_routing=False,
    )
    with factory.begin() as session:
        session.add(
            ResearchScheduleEventRow(
                event_id="synthetic-expired-execution-lease",
                work_item_id=work_item_id,
                sequence=4,
                event_type=(
                    ResearchScheduleEventType
                    .EXECUTION_LEASE_ACQUIRED.value
                ),
                attempt_number=1,
                retryable=True,
                lease_owner=stale_lease.consumer_id,
                lease_token=stale_lease.execution_token,
                lease_expires_at=stale_lease.lease_expires_at,
                receipt_id=receipt_id,
                idempotency_key="synthetic-expired-execution-lease",
                event_hash="f" * 64,
                config_manifest_hash=config_hash,
                real_order_routing=False,
                payload_json={
                    "reason": "synthetic expired execution lease"
                },
                created_at=stale_lease.acquired_at,
            )
        )

    replacement = _claim_execution(
        repository,
        consumer_id="replacement-consumer",
    )
    assert replacement.receipt_id == receipt_id
    assert repository.event_history(work_item_id)[-1].event_type == (
        ResearchScheduleEventType.EXECUTION_LEASE_RECLAIMED.value
    )
    with pytest.raises(ResearchScheduleFenceError, match="stale"):
        repository.record_execution_outcome(
            execution_lease=stale_lease,
            succeeded=True,
            reason_code=None,
            maximum_attempts=3,
            result=_maintenance_result(repository, replacement),
        )
    assert repository.record_execution_outcome(
        execution_lease=replacement,
        succeeded=True,
        reason_code=None,
        maximum_attempts=3,
        result=_maintenance_result(repository, replacement),
    )


def test_scheduler_failure_does_not_touch_operational_plane_rows(
    sqlite_database,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, factory = sqlite_database
    _seed_calendar(factory)
    repository, service = _service(factory, repository_root)
    service.plan(as_of=PLAN_AS_OF)
    with factory() as session:
        before = {
            "cycles": session.scalar(
                select(func.count()).select_from(PaperCycleRow)
            ),
            "orders": session.scalar(
                select(func.count()).select_from(OrderIntentRow)
            ),
            "fills": session.scalar(
                select(func.count()).select_from(FillRow)
            ),
        }

    def fail_receipt(*, lease: ResearchWorkLeaseV1):
        del lease
        raise RuntimeError("synthetic dispatch preparation failure")

    monkeypatch.setattr(repository, "commit_dispatch", fail_receipt)
    result = service.dispatch_once(worker_id="failing-scheduler")
    assert result["failed"] is True
    assert result["reason_code"] == "RUNTIMEERROR"

    with factory() as session:
        after = {
            "cycles": session.scalar(
                select(func.count()).select_from(PaperCycleRow)
            ),
            "orders": session.scalar(
                select(func.count()).select_from(OrderIntentRow)
            ),
            "fills": session.scalar(
                select(func.count()).select_from(FillRow)
            ),
        }
        failure = session.scalar(
            select(ResearchScheduleEventRow)
            .where(
                ResearchScheduleEventRow.event_type
                == ResearchScheduleEventType.FAILED.value
            )
            .order_by(ResearchScheduleEventRow.sequence.desc())
        )
    assert before == after == {"cycles": 0, "orders": 0, "fills": 0}
    assert failure is not None
    assert failure.payload_json["reason_code"] == "RUNTIMEERROR"
    assert "synthetic dispatch preparation failure" not in str(
        failure.payload_json
    )


def test_research_scheduler_cli_plans_dispatches_and_reports_status(
    sqlite_database,
    repository_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url, _, factory = sqlite_database
    _seed_calendar(factory)
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "TRADING_CONFIG_DIR",
        str(repository_root / "config"),
    )
    monkeypatch.setenv("TRADING_RAW_STORE", str(tmp_path / "raw"))
    monkeypatch.setenv(
        "TRADING_PAPER_ACCOUNT_FILE",
        str(repository_root / "config" / "paper-account.example.yaml"),
    )
    runner = CliRunner()

    planned = runner.invoke(
        app,
        [
            "research",
            "schedule-plan",
            "--as-of",
            PLAN_AS_OF.isoformat(),
        ],
    )
    assert planned.exit_code == 0, planned.output
    assert planned.stdout
    work = runner.invoke(
        app,
        [
            "research",
            "schedule-work",
            "--worker-id",
            "cli-scheduler",
            "--as-of",
            PLAN_AS_OF.isoformat(),
        ],
    )
    assert work.exit_code == 0, work.output
    assert '"dispatched": true' in work.stdout.lower()
    executor = tmp_path / "cli-executor.py"
    executor.write_text(
        """
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from trading.research.dispatch_execution import (
    ResearchExecutionArtifactKind,
    ResearchExecutionArtifactV1,
    ResearchWorkExecutionRequestV1,
    build_execution_result,
)
parser = argparse.ArgumentParser()
parser.add_argument("--request", type=Path, required=True)
parser.add_argument("--result", type=Path, required=True)
args = parser.parse_args()
request = ResearchWorkExecutionRequestV1.model_validate(
    json.loads(args.request.read_text(encoding="utf-8"))
)
result = build_execution_result(
    request=request,
    artifacts=(ResearchExecutionArtifactV1(
        artifact_kind=ResearchExecutionArtifactKind.DAILY_AGGREGATION,
        content_hash="a" * 64,
        record_count=1,
    ),),
    invocations=(),
    decision_kind=None,
    completed_at=datetime.now(UTC),
)
args.result.parent.mkdir(parents=True, exist_ok=True)
args.result.write_text(
    json.dumps(result.model_dump(mode="json"), sort_keys=True),
    encoding="utf-8",
)
""".lstrip(),
        encoding="utf-8",
    )
    consumed = runner.invoke(
        app,
        [
            "research",
            "schedule-consume",
            "--executor",
            str(executor),
            "--artifact-root",
            str(tmp_path / "dispatch-artifacts"),
            "--timeout-seconds",
            "30",
            "--consumer-id",
            "cli-consumer",
        ],
    )
    assert consumed.exit_code == 0, consumed.output
    assert '"consumed": true' in consumed.stdout.lower()

    status = runner.invoke(app, ["research", "status"])
    assert status.exit_code == 0, status.output
    assert '"scheduler"' in status.stdout
    assert '"real_order_routing": false' in status.stdout.lower()
