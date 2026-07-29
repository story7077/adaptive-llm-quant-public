from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.domain.hashing import canonical_hash, stable_id
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
    build_execution_request,
    build_execution_result,
)
from trading.research.scheduler import (
    ResearchSchedulePlanV1,
    ResearchScheduleWorkKind,
    ResearchWorkExecutionLeaseV1,
    ResearchWorkLeaseV1,
    build_dispatch_receipt,
)

AS_OF = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
CONFIG_HASH = "a" * 64


def _deep_request(
    selected_commander: ResearchCommanderKind,
):
    trigger_hash = canonical_hash(
        {"source_ids": (), "content_hashes": ()}
    )
    plan_payload = {
        "schema_version": "research_schedule_plan_v1",
        "work_item_id": "weekly-work-001",
        "work_kind": ResearchScheduleWorkKind.WEEKLY_DEEP_RESEARCH,
        "idempotency_key": "weekly-work-idempotency-001",
        "schedule_version": "research_schedule_v1",
        "scheduled_for": AS_OF,
        "data_available_cutoff": AS_OF,
        "calendar_session_id": "calendar-session-001",
        "calendar_session_hash": "b" * 64,
        "calendar_version": "alpaca_market_calendar_v1",
        "trigger_source_ids": (),
        "trigger_content_hashes": (),
        "trigger_manifest_hash": trigger_hash,
        "config_manifest_hash": CONFIG_HASH,
        "real_order_routing": False,
    }
    plan = ResearchSchedulePlanV1.model_validate(
        {
            **plan_payload,
            "plan_hash": canonical_hash(plan_payload),
        }
    )
    dispatch_lease = ResearchWorkLeaseV1(
        work_item_id=plan.work_item_id,
        work_kind=plan.work_kind,
        lease_owner="scheduler",
        lease_token="dispatch-token",
        attempt_number=1,
        acquired_at=AS_OF,
        lease_expires_at=AS_OF + timedelta(minutes=15),
        config_manifest_hash=CONFIG_HASH,
        plan_hash=plan.plan_hash,
        real_order_routing=False,
    )
    receipt = build_dispatch_receipt(
        plan=plan,
        lease=dispatch_lease,
        created_at=AS_OF,
    )
    execution_lease = ResearchWorkExecutionLeaseV1(
        execution_id=stable_id(
            "research-work-execution",
            receipt.receipt_id,
        ),
        receipt_id=receipt.receipt_id,
        receipt_hash=receipt.receipt_hash,
        work_item_id=plan.work_item_id,
        work_kind=plan.work_kind,
        consumer_id="consumer",
        execution_token="execution-token",
        dispatch_attempt_number=1,
        acquired_at=AS_OF,
        lease_expires_at=AS_OF + timedelta(minutes=15),
        config_manifest_hash=CONFIG_HASH,
        real_order_routing=False,
    )
    selection = CommanderSelectionV1(
        selection_id="selection-001",
        version=1,
        selected_commander=selected_commander,
        effective_at=AS_OF - timedelta(minutes=1),
        created_at=AS_OF - timedelta(minutes=1),
        config_hash="c" * 64,
    )
    return build_execution_request(
        execution_lease=execution_lease,
        plan=plan,
        receipt=receipt,
        commander_selection=selection,
    )


def _attestation(
    *,
    role: ResearchExecutionRole,
    access_path: ResearchExecutionAccessPath,
    model_family: str,
    reasoning_profile: str,
    context: str,
) -> ResearchInvocationAttestationV1:
    return ResearchInvocationAttestationV1(
        role=role,
        access_path=access_path,
        model_family=model_family,
        reasoning_profile=reasoning_profile,
        invocation_context_hash=canonical_hash({"context": context}),
        request_hash=canonical_hash({"request": context}),
        output_hash=canonical_hash({"output": context}),
        fresh_process=True,
        fresh_context=True,
        completed=True,
        api_fallback_used=False,
    )


def _base_artifacts() -> tuple[ResearchExecutionArtifactV1, ...]:
    return (
        ResearchExecutionArtifactV1(
            artifact_kind=(
                ResearchExecutionArtifactKind.WEB_RESEARCH_EVIDENCE
            ),
            content_hash="d" * 64,
            record_count=14,
        ),
        ResearchExecutionArtifactV1(
            artifact_kind=ResearchExecutionArtifactKind.RESEARCH_DECISION,
            content_hash="e" * 64,
            record_count=1,
        ),
    )


def test_codex_deep_result_requires_exact_fresh_scout_and_commander_routes() -> None:
    request = _deep_request(ResearchCommanderKind.CODEX_SOL_MAX)
    result = build_execution_result(
        request=request,
        artifacts=_base_artifacts(),
        invocations=(
            _attestation(
                role=ResearchExecutionRole.WEB_SCOUT,
                access_path=(
                    ResearchExecutionAccessPath.CHATGPT_WEB_AGBROWSE
                ),
                model_family="GPT-5.6 Sol Pro",
                reasoning_profile="xhigh",
                context="scout-fresh",
            ),
            _attestation(
                role=ResearchExecutionRole.RESEARCH_COMMANDER,
                access_path=ResearchExecutionAccessPath.CODEX_EPHEMERAL,
                model_family="gpt-5.6-sol",
                reasoning_profile="max",
                context="commander-fresh",
            ),
        ),
        decision_kind=ResearchDecisionKind.NO_RESEARCH_CHANGE,
        completed_at=AS_OF + timedelta(minutes=5),
    )

    assert result.research_cycle_id == request.research_cycle_id
    assert result.selected_commander is ResearchCommanderKind.CODEX_SOL_MAX
    assert len(result.invocations) == 2
    assert result.real_order_routing is False
    assert result.automatic_promotion_enabled is False


def test_deep_result_rejects_shared_invocation_context() -> None:
    request = _deep_request(ResearchCommanderKind.WEBGPT_SOL_PRO)
    scout = _attestation(
        role=ResearchExecutionRole.WEB_SCOUT,
        access_path=ResearchExecutionAccessPath.CHATGPT_WEB_AGBROWSE,
        model_family="GPT-5.6 Sol Pro",
        reasoning_profile="xhigh",
        context="shared-context",
    )
    commander = scout.model_copy(
        update={"role": ResearchExecutionRole.RESEARCH_COMMANDER}
    )

    with pytest.raises(ValueError, match="contexts must be unique"):
        build_execution_result(
            request=request,
            artifacts=_base_artifacts(),
            invocations=(scout, commander),
            decision_kind=ResearchDecisionKind.NO_RESEARCH_CHANGE,
            completed_at=AS_OF + timedelta(minutes=5),
        )


def test_proposal_result_requires_separate_codex_builder_and_candidate_artifacts() -> None:
    request = _deep_request(ResearchCommanderKind.WEBGPT_SOL_PRO)
    scout = _attestation(
        role=ResearchExecutionRole.WEB_SCOUT,
        access_path=ResearchExecutionAccessPath.CHATGPT_WEB_AGBROWSE,
        model_family="GPT-5.6 Sol Pro",
        reasoning_profile="xhigh",
        context="scout-fresh",
    )
    commander = _attestation(
        role=ResearchExecutionRole.RESEARCH_COMMANDER,
        access_path=ResearchExecutionAccessPath.CHATGPT_WEB_AGBROWSE,
        model_family="GPT-5.6 Sol Pro",
        reasoning_profile="xhigh",
        context="commander-separate-fresh",
    )

    with pytest.raises(ValueError, match="Builder invocation"):
        build_execution_result(
            request=request,
            artifacts=_base_artifacts(),
            invocations=(scout, commander),
            decision_kind=(
                ResearchDecisionKind.PROPOSE_STRATEGY_REVISION
            ),
            completed_at=AS_OF + timedelta(minutes=5),
        )

    result = build_execution_result(
        request=request,
        artifacts=(
            *_base_artifacts(),
            ResearchExecutionArtifactV1(
                artifact_kind=(
                    ResearchExecutionArtifactKind.ALGORITHM_PROPOSAL
                ),
                content_hash="f" * 64,
                record_count=1,
            ),
            ResearchExecutionArtifactV1(
                artifact_kind=(
                    ResearchExecutionArtifactKind.CANDIDATE_MANIFEST
                ),
                content_hash="1" * 64,
                record_count=1,
            ),
        ),
        invocations=(
            scout,
            commander,
            _attestation(
                role=ResearchExecutionRole.CANDIDATE_BUILDER,
                access_path=ResearchExecutionAccessPath.CODEX_EPHEMERAL,
                model_family="gpt-5.6-sol",
                reasoning_profile="max",
                context="builder-third-fresh",
            ),
        ),
        decision_kind=ResearchDecisionKind.PROPOSE_STRATEGY_REVISION,
        completed_at=AS_OF + timedelta(minutes=10),
    )

    assert [item.role for item in result.invocations] == [
        ResearchExecutionRole.WEB_SCOUT,
        ResearchExecutionRole.RESEARCH_COMMANDER,
        ResearchExecutionRole.CANDIDATE_BUILDER,
    ]
