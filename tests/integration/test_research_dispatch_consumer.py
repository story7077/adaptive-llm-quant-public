from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from trading.domain.hashing import canonical_hash
from trading.persistence.models import MarketCalendarSessionRow
from trading.persistence.research_scheduler import (
    ResearchScheduleFenceError,
    ResearchSchedulerRepository,
)
from trading.research.config import ResearchConfigBundle, load_research_config
from trading.research.contracts import (
    CommanderSelectionV1,
    ResearchCommanderKind,
    ResearchDecisionKind,
)
from trading.research.dispatch_execution import (
    ResearchExecutionAccessPath,
    ResearchExecutionArtifactKind,
    ResearchExecutionArtifactV1,
    ResearchExecutionRole,
    ResearchInvocationAttestationV1,
    ResearchWorkExecutionResultV1,
    build_execution_request,
    build_execution_result,
)
from trading.research.scheduler import (
    ResearchScheduleEventType,
    ResearchSchedulePlanV1,
    ResearchScheduleWorkKind,
    ResearchWorkExecutionLeaseV1,
)
from trading.runtime.research_dispatch_consumer import (
    ResearchDispatchConsumerService,
    SubprocessResearchDispatchExecutor,
)
from trading.runtime.research_scheduler import ResearchSchedulerService

SESSION_DATE = date(2020, 7, 24)
SESSION_OPEN = datetime(2020, 7, 24, 13, 30, tzinfo=UTC)
SESSION_CLOSE = datetime(2020, 7, 24, 20, 0, tzinfo=UTC)
PLAN_AS_OF = datetime(2020, 7, 25, 2, 0, tzinfo=UTC)


def _seed_calendar(factory) -> None:
    payload = {
        "calendar_session_id": "calendar-consumer-2020-07-24",
        "calendar_version": "alpaca_market_calendar_v1",
        "session_date": SESSION_DATE.isoformat(),
        "open_at": SESSION_OPEN.isoformat(),
        "close_at": SESSION_CLOSE.isoformat(),
        "available_at": datetime(
            2020,
            7,
            1,
            tzinfo=UTC,
        ).isoformat(),
    }
    payload["session_hash"] = canonical_hash(payload)
    with factory.begin() as session:
        session.add(
            MarketCalendarSessionRow(
                calendar_session_id=str(payload["calendar_session_id"]),
                algorithm_version="q1_math_core_v1",
                calendar_version=str(payload["calendar_version"]),
                session_date=SESSION_DATE,
                open_at=SESSION_OPEN,
                close_at=SESSION_CLOSE,
                available_at=datetime(2020, 7, 1, tzinfo=UTC),
                source="SYNTHETIC_TEST",
                config_manifest_hash="a" * 64,
                code_version="test-v1",
                model_version="none",
                source_manifest_hash="b" * 64,
                session_hash=str(payload["session_hash"]),
                payload_json=payload,
                created_at=datetime(2020, 7, 1, tzinfo=UTC),
            )
        )


def _dispatched_repository(factory, repository_root: Path):
    config = load_research_config(repository_root / "config")
    repository = ResearchSchedulerRepository(factory)
    scheduler = ResearchSchedulerService(
        repository=repository,
        config=config,
    )
    scheduler.plan(as_of=PLAN_AS_OF)
    dispatch = scheduler.dispatch_once(worker_id="dispatch-worker")
    assert dispatch["dispatched"] is True
    return repository, config, dispatch["receipt"]


def _write_success_executor(path: Path) -> None:
    path.write_text(
        """
from __future__ import annotations

import argparse
import json
import os
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
if "APCA_API_SECRET_KEY" in os.environ:
    raise SystemExit(7)
request = ResearchWorkExecutionRequestV1.model_validate(
    json.loads(args.request.read_text(encoding="utf-8"))
)
result = build_execution_result(
    request=request,
    artifacts=(
        ResearchExecutionArtifactV1(
            artifact_kind=ResearchExecutionArtifactKind.DAILY_AGGREGATION,
            content_hash="a" * 64,
            record_count=3,
        ),
    ),
    invocations=(),
    decision_kind=None,
    completed_at=datetime.now(UTC),
)
args.result.parent.mkdir(parents=True, exist_ok=True)
temporary = args.result.with_suffix(".tmp")
temporary.write_text(
    json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
    ),
    encoding="utf-8",
)
temporary.replace(args.result)
""".lstrip(),
        encoding="utf-8",
    )


def _write_failure_executor(path: Path) -> None:
    path.write_text(
        "raise SystemExit(9)\n",
        encoding="utf-8",
    )


def test_consumer_executes_typed_result_without_inheriting_broker_secret(
    sqlite_database,
    repository_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    _, _, factory = sqlite_database
    _seed_calendar(factory)
    repository, config, receipt = _dispatched_repository(
        factory,
        repository_root,
    )
    executable = tmp_path / "executor.py"
    _write_success_executor(executable)
    monkeypatch.setenv("APCA_API_SECRET_KEY", "must-not-reach-executor")
    artifact_root = tmp_path / "dispatch-artifacts"
    consumer = ResearchDispatchConsumerService(
        repository=repository,
        config=config,
        executor=SubprocessResearchDispatchExecutor(
            executable=executable,
            artifact_root=artifact_root,
            timeout_seconds=30,
        ),
        selection_provider=lambda: None,
    )

    result = asyncio.run(
        consumer.consume_once(consumer_id="consumer-1")
    )

    assert result["consumed"] is True
    assert result["artifact_count"] == 1
    assert result["invocation_count"] == 0
    assert result["real_order_routing"] is False
    assert result["automatic_promotion_enabled"] is False
    assert asyncio.run(
        consumer.consume_once(consumer_id="consumer-1")
    )["reason_code"] == "NO_DISPATCHED_RESEARCH_WORK"
    history = repository.event_history(str(receipt["work_item_id"]))
    assert tuple(row.event_type for row in history[-2:]) == (
        ResearchScheduleEventType.EXECUTION_LEASE_ACQUIRED.value,
        ResearchScheduleEventType.SUCCEEDED.value,
    )
    stored_result = history[-1].payload_json["execution_result"]
    assert stored_result["result_hash"] == result["result_hash"]
    request_text = next(
        artifact_root.rglob("request.json")
    ).read_text(encoding="utf-8")
    assert "execution_token" not in request_text
    assert "APCA_API_SECRET_KEY" not in request_text
    assert "must-not-reach-executor" not in request_text


def test_consumer_failure_is_sanitized_and_retryable(
    sqlite_database,
    repository_root: Path,
    tmp_path: Path,
) -> None:
    _, _, factory = sqlite_database
    _seed_calendar(factory)
    repository, config, receipt = _dispatched_repository(
        factory,
        repository_root,
    )
    executable = tmp_path / "failure.py"
    _write_failure_executor(executable)
    consumer = ResearchDispatchConsumerService(
        repository=repository,
        config=config,
        executor=SubprocessResearchDispatchExecutor(
            executable=executable,
            artifact_root=tmp_path / "dispatch-artifacts",
            timeout_seconds=30,
        ),
        selection_provider=lambda: None,
    )

    result = asyncio.run(
        consumer.consume_once(consumer_id="consumer-failure")
    )

    assert result["failed"] is True
    assert result["reason_code"] == "EXECUTOR_EXIT_NONZERO"
    history = repository.event_history(str(receipt["work_item_id"]))
    assert history[-1].event_type == ResearchScheduleEventType.FAILED.value
    assert history[-1].retryable is True
    serialized = json.dumps(history[-1].payload_json, sort_keys=True)
    assert "SystemExit" not in serialized
    retry = ResearchSchedulerService(
        repository=repository,
        config=config,
    ).dispatch_once(worker_id="retry-dispatch-worker")
    assert retry["dispatched"] is True
    assert retry["receipt"]["attempt_number"] == 2


def test_execution_lease_renewal_is_append_only_and_keeps_token(
    sqlite_database,
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    _seed_calendar(factory)
    repository, _, receipt = _dispatched_repository(
        factory,
        repository_root,
    )
    lease = repository.claim_execution(
        consumer_id="renewing-consumer",
        lease_seconds=900,
    )
    assert lease is not None

    renewed = repository.renew_execution_lease(
        execution_lease=lease,
        lease_seconds=900,
    )

    assert renewed.execution_id == lease.execution_id
    assert renewed.execution_token == lease.execution_token
    assert renewed.lease_expires_at >= lease.lease_expires_at
    history = repository.event_history(str(receipt["work_item_id"]))
    assert history[-1].event_type == (
        ResearchScheduleEventType.EXECUTION_LEASE_RENEWED.value
    )


def test_second_deep_cycle_cannot_reuse_prior_model_context(
    sqlite_database,
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    _seed_calendar(factory)
    repository = ResearchSchedulerRepository(factory)
    config = load_research_config(repository_root / "config")
    plans = (
        _deep_plan("deep-work-1", offset_minutes=0),
        _deep_plan("deep-work-2", offset_minutes=1),
    )
    assert repository.store_plans(
        plans,
        created_at=SESSION_CLOSE,
    ) == 2
    selection = CommanderSelectionV1(
        selection_id="selection-deep-001",
        version=1,
        selected_commander=ResearchCommanderKind.CODEX_SOL_MAX,
        effective_at=SESSION_OPEN,
        created_at=SESSION_OPEN,
        config_hash="c" * 64,
    )

    first_result, first_lease = _complete_deep_dispatch(
        repository=repository,
        config=config,
        selection=selection,
        context_suffix="shared-scout",
        worker_id="deep-worker-1",
        consumer_id="deep-consumer-1",
    )
    assert repository.record_execution_outcome(
        execution_lease=first_lease,
        succeeded=True,
        reason_code=None,
        maximum_attempts=3,
        result=first_result,
    )

    second_result, second_lease = _complete_deep_dispatch(
        repository=repository,
        config=config,
        selection=selection,
        context_suffix="shared-scout",
        worker_id="deep-worker-2",
        consumer_id="deep-consumer-2",
    )
    with pytest.raises(
        ResearchScheduleFenceError,
        match="reused an earlier model context",
    ):
        repository.record_execution_outcome(
            execution_lease=second_lease,
            succeeded=True,
            reason_code=None,
            maximum_attempts=3,
            result=second_result,
        )


def _deep_plan(
    identity: str,
    *,
    offset_minutes: int,
) -> ResearchSchedulePlanV1:
    scheduled_for = SESSION_CLOSE + timedelta(minutes=offset_minutes)
    trigger_hash = canonical_hash(
        {"source_ids": (), "content_hashes": ()}
    )
    payload = {
        "schema_version": "research_schedule_plan_v1",
        "work_item_id": identity,
        "work_kind": ResearchScheduleWorkKind.WEEKLY_DEEP_RESEARCH,
        "idempotency_key": f"{identity}-idempotency",
        "schedule_version": "research_schedule_v1",
        "scheduled_for": scheduled_for,
        "data_available_cutoff": scheduled_for,
        "calendar_session_id": "calendar-consumer-2020-07-24",
        "calendar_session_hash": "b" * 64,
        "calendar_version": "alpaca_market_calendar_v1",
        "trigger_source_ids": (),
        "trigger_content_hashes": (),
        "trigger_manifest_hash": trigger_hash,
        "config_manifest_hash": "a" * 64,
        "real_order_routing": False,
    }
    return ResearchSchedulePlanV1.model_validate(
        {**payload, "plan_hash": canonical_hash(payload)}
    )


def _complete_deep_dispatch(
    *,
    repository: ResearchSchedulerRepository,
    config: ResearchConfigBundle,
    selection: CommanderSelectionV1,
    context_suffix: str,
    worker_id: str,
    consumer_id: str,
) -> tuple[
    ResearchWorkExecutionResultV1,
    ResearchWorkExecutionLeaseV1,
]:
    lease = repository.claim_next(
        lease_owner=worker_id,
        lease_seconds=config.config.schedule.dispatch_lease_seconds,
        maximum_attempts=3,
    )
    assert lease is not None
    repository.commit_dispatch(lease=lease)
    execution_lease = repository.claim_execution(
        consumer_id=consumer_id,
        lease_seconds=900,
    )
    assert execution_lease is not None
    plan, receipt = repository.execution_input(
        execution_lease=execution_lease
    )
    request = build_execution_request(
        execution_lease=execution_lease,
        plan=plan,
        receipt=receipt,
        commander_selection=selection,
    )
    attestations = (
        _model_attestation(
            role=ResearchExecutionRole.WEB_SCOUT,
            access_path=(
                ResearchExecutionAccessPath.CHATGPT_WEB_AGBROWSE
            ),
            model_family="GPT-5.6 Sol Pro",
            reasoning_profile="xhigh",
            context_suffix=context_suffix,
        ),
        _model_attestation(
            role=ResearchExecutionRole.RESEARCH_COMMANDER,
            access_path=ResearchExecutionAccessPath.CODEX_EPHEMERAL,
            model_family="gpt-5.6-sol",
            reasoning_profile="max",
            context_suffix=f"{consumer_id}-commander",
        ),
    )
    result = build_execution_result(
        request=request,
        artifacts=(
            ResearchExecutionArtifactV1(
                artifact_kind=(
                    ResearchExecutionArtifactKind
                    .WEB_RESEARCH_EVIDENCE
                ),
                content_hash=canonical_hash(
                    {"evidence": execution_lease.execution_id}
                ),
                record_count=1,
            ),
            ResearchExecutionArtifactV1(
                artifact_kind=(
                    ResearchExecutionArtifactKind.RESEARCH_DECISION
                ),
                content_hash=canonical_hash(
                    {"decision": execution_lease.execution_id}
                ),
                record_count=1,
            ),
        ),
        invocations=attestations,
        decision_kind=ResearchDecisionKind.NO_RESEARCH_CHANGE,
        completed_at=execution_lease.acquired_at,
    )
    return result, execution_lease


def _model_attestation(
    *,
    role: ResearchExecutionRole,
    access_path: ResearchExecutionAccessPath,
    model_family: str,
    reasoning_profile: str,
    context_suffix: str,
) -> ResearchInvocationAttestationV1:
    return ResearchInvocationAttestationV1(
        role=role,
        access_path=access_path,
        model_family=model_family,
        reasoning_profile=reasoning_profile,
        invocation_context_hash=canonical_hash(
            {"context": context_suffix}
        ),
        request_hash=canonical_hash({"request": context_suffix}),
        output_hash=canonical_hash({"output": context_suffix}),
        fresh_process=True,
        fresh_context=True,
        completed=True,
        api_fallback_used=False,
    )
