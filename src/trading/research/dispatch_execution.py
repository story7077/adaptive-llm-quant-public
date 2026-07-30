from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.research.contracts import (
    CommanderSelectionV1,
    ResearchCommanderKind,
    ResearchDecisionKind,
)
from trading.research.scheduler import (
    ResearchDispatchTarget,
    ResearchSchedulePlanV1,
    ResearchScheduleWorkKind,
    ResearchWorkDispatchReceiptV1,
    ResearchWorkExecutionLeaseV1,
    schedule_plan_payload_hash,
)

SHA256_PATTERN = r"^[a-f0-9]{64}$"
EXECUTION_REQUEST_SCHEMA_VERSION = "research_work_execution_request_v1"
EXECUTION_RESULT_SCHEMA_VERSION = "research_work_execution_result_v1"


class ResearchExecutionArtifactKind(StrEnum):
    DAILY_AGGREGATION = "DAILY_AGGREGATION"
    OUTCOME_MATURATION = "OUTCOME_MATURATION"
    RESEARCH_MEMORY_SNAPSHOT = "RESEARCH_MEMORY_SNAPSHOT"
    WEB_RESEARCH_EVIDENCE = "WEB_RESEARCH_EVIDENCE"
    RESEARCH_DECISION = "RESEARCH_DECISION"
    ALGORITHM_PROPOSAL = "ALGORITHM_PROPOSAL"
    CANDIDATE_MANIFEST = "CANDIDATE_MANIFEST"


class ResearchExecutionRole(StrEnum):
    WEB_SCOUT = "WEB_SCOUT"
    RESEARCH_COMMANDER = "RESEARCH_COMMANDER"
    CANDIDATE_BUILDER = "CANDIDATE_BUILDER"


class ResearchExecutionAccessPath(StrEnum):
    CHATGPT_WEB_AGBROWSE = "CHATGPT_WEB_AGBROWSE"
    CODEX_EPHEMERAL = "CODEX_EPHEMERAL"


class ResearchExecutionArtifactV1(DomainModel):
    artifact_kind: ResearchExecutionArtifactKind
    content_hash: str = Field(pattern=SHA256_PATTERN)
    record_count: int = Field(ge=0)


class ResearchInvocationAttestationV1(DomainModel):
    role: ResearchExecutionRole
    access_path: ResearchExecutionAccessPath
    model_family: str = Field(min_length=1, max_length=80)
    reasoning_profile: str = Field(min_length=1, max_length=20)
    invocation_context_hash: str = Field(pattern=SHA256_PATTERN)
    request_hash: str = Field(pattern=SHA256_PATTERN)
    output_hash: str = Field(pattern=SHA256_PATTERN)
    fresh_process: Literal[True] = True
    fresh_context: Literal[True] = True
    completed: Literal[True] = True
    api_fallback_used: Literal[False] = False

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        if self.role is ResearchExecutionRole.WEB_SCOUT:
            expected = (
                ResearchExecutionAccessPath.CHATGPT_WEB_AGBROWSE,
                "GPT-5.6 Sol Pro",
                "xhigh",
            )
        elif self.role is ResearchExecutionRole.CANDIDATE_BUILDER:
            expected = (
                ResearchExecutionAccessPath.CODEX_EPHEMERAL,
                "gpt-5.6-sol",
                "max",
            )
        else:
            return self
        if (
            self.access_path,
            self.model_family,
            self.reasoning_profile,
        ) != expected:
            raise ValueError(f"{self.role.value} model route mismatch")
        return self


class ResearchWorkExecutionRequestV1(DomainModel):
    schema_version: Literal[
        "research_work_execution_request_v1"
    ] = EXECUTION_REQUEST_SCHEMA_VERSION
    execution_id: str = Field(min_length=1, max_length=100)
    research_cycle_id: str = Field(min_length=1, max_length=100)
    plan: ResearchSchedulePlanV1
    receipt: ResearchWorkDispatchReceiptV1
    commander_selection: CommanderSelectionV1 | None
    config_manifest_hash: str = Field(pattern=SHA256_PATTERN)
    request_hash: str = Field(pattern=SHA256_PATTERN)
    automatic_promotion_enabled: Literal[False] = False
    real_order_routing: Literal[False] = False

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        expected_execution_id = stable_id(
            "research-work-execution",
            self.receipt.receipt_id,
        )
        if (
            self.execution_id != expected_execution_id
            or self.plan.work_item_id != self.receipt.work_item_id
            or self.plan.work_kind is not self.receipt.work_kind
            or self.receipt.work_payload_hash
            != schedule_plan_payload_hash(self.plan)
            or self.config_manifest_hash != self.plan.config_manifest_hash
            or self.config_manifest_hash != self.receipt.config_manifest_hash
        ):
            raise ValueError("research execution request binding mismatch")
        expected_cycle = stable_id(
            "scheduled-research-cycle",
            self.execution_id,
        )
        if self.research_cycle_id != expected_cycle:
            raise ValueError("research execution cycle identity mismatch")
        is_deep = (
            self.receipt.dispatch_target
            is ResearchDispatchTarget.DEEP_RESEARCH_CYCLE_V1
        )
        if is_deep != (self.commander_selection is not None):
            raise ValueError(
                "deep research execution requires one Commander selection"
            )
        payload = self.model_dump(mode="python", exclude={"request_hash"})
        if canonical_hash(payload) != self.request_hash:
            raise ValueError("research execution request hash mismatch")
        return self


class ResearchWorkExecutionResultV1(DomainModel):
    schema_version: Literal[
        "research_work_execution_result_v1"
    ] = EXECUTION_RESULT_SCHEMA_VERSION
    execution_id: str = Field(min_length=1, max_length=100)
    request_hash: str = Field(pattern=SHA256_PATTERN)
    receipt_id: str = Field(min_length=1, max_length=100)
    work_item_id: str = Field(min_length=1, max_length=100)
    work_kind: ResearchScheduleWorkKind
    dispatch_target: ResearchDispatchTarget
    research_cycle_id: str | None
    commander_selection_id: str | None
    commander_selection_version: int | None = Field(default=None, ge=1)
    selected_commander: ResearchCommanderKind | None
    decision_kind: ResearchDecisionKind | None
    artifacts: tuple[ResearchExecutionArtifactV1, ...] = Field(
        min_length=1,
        max_length=20,
    )
    invocations: tuple[ResearchInvocationAttestationV1, ...] = Field(
        max_length=3,
    )
    completed_at: datetime
    result_hash: str = Field(pattern=SHA256_PATTERN)
    automatic_promotion_enabled: Literal[False] = False
    real_order_routing: Literal[False] = False

    @field_validator("completed_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if len({item.artifact_kind for item in self.artifacts}) != len(
            self.artifacts
        ):
            raise ValueError("research execution artifact kinds must be unique")
        if len({item.role for item in self.invocations}) != len(self.invocations):
            raise ValueError("research execution invocation roles must be unique")
        if len(
            {
                item.invocation_context_hash
                for item in self.invocations
            }
        ) != len(self.invocations):
            raise ValueError(
                "research execution invocation contexts must be unique"
            )
        is_deep = (
            self.dispatch_target
            is ResearchDispatchTarget.DEEP_RESEARCH_CYCLE_V1
        )
        selection_fields = (
            self.commander_selection_id,
            self.commander_selection_version,
            self.selected_commander,
        )
        if is_deep:
            if self.research_cycle_id is None or any(
                value is None for value in selection_fields
            ):
                raise ValueError(
                    "deep research result requires cycle and Commander binding"
                )
            if self.decision_kind is None:
                raise ValueError("deep research result requires a decision kind")
            self._validate_deep_invocations()
            self._validate_deep_artifacts()
        elif (
            self.research_cycle_id is not None
            or any(value is not None for value in selection_fields)
            or self.decision_kind is not None
            or self.invocations
        ):
            raise ValueError(
                "maintenance result cannot contain model-cycle fields"
            )
        else:
            expected_artifact = {
                ResearchDispatchTarget.DAILY_AGGREGATION_V1: (
                    ResearchExecutionArtifactKind.DAILY_AGGREGATION
                ),
                ResearchDispatchTarget.OUTCOME_MATURATION_V1: (
                    ResearchExecutionArtifactKind.OUTCOME_MATURATION
                ),
                (
                    ResearchDispatchTarget
                    .RESEARCH_MEMORY_MATERIALIZATION_V1
                ): (
                    ResearchExecutionArtifactKind
                    .RESEARCH_MEMORY_SNAPSHOT
                ),
            }[self.dispatch_target]
            if tuple(item.artifact_kind for item in self.artifacts) != (
                expected_artifact,
            ):
                raise ValueError(
                    "maintenance result artifact does not match dispatch target"
                )
        payload = self.model_dump(mode="python", exclude={"result_hash"})
        if canonical_hash(payload) != self.result_hash:
            raise ValueError("research execution result hash mismatch")
        return self

    def assert_bound_to(
        self,
        request: ResearchWorkExecutionRequestV1,
    ) -> None:
        if (
            self.execution_id != request.execution_id
            or self.request_hash != request.request_hash
            or self.receipt_id != request.receipt.receipt_id
            or self.work_item_id != request.plan.work_item_id
            or self.work_kind is not request.plan.work_kind
            or self.dispatch_target is not request.receipt.dispatch_target
        ):
            raise ValueError("research execution result binding mismatch")
        selection = request.commander_selection
        if selection is None:
            if (
                self.commander_selection_id is not None
                or self.commander_selection_version is not None
                or self.selected_commander is not None
            ):
                raise ValueError("unexpected Commander result binding")
        elif (
            self.research_cycle_id != request.research_cycle_id
            or self.commander_selection_id != selection.selection_id
            or self.commander_selection_version != selection.version
            or self.selected_commander is not selection.selected_commander
        ):
            raise ValueError("stale Commander selection in execution result")

    def _validate_deep_invocations(self) -> None:
        by_role = {item.role: item for item in self.invocations}
        required = {
            ResearchExecutionRole.WEB_SCOUT,
            ResearchExecutionRole.RESEARCH_COMMANDER,
        }
        if not required.issubset(by_role):
            raise ValueError(
                "deep research result requires fresh Scout and Commander invocations"
            )
        commander = by_role[ResearchExecutionRole.RESEARCH_COMMANDER]
        if self.selected_commander is ResearchCommanderKind.CODEX_SOL_MAX:
            expected = (
                ResearchExecutionAccessPath.CODEX_EPHEMERAL,
                "gpt-5.6-sol",
                "max",
            )
        else:
            expected = (
                ResearchExecutionAccessPath.CHATGPT_WEB_AGBROWSE,
                "GPT-5.6 Sol Pro",
                "xhigh",
            )
        if (
            commander.access_path,
            commander.model_family,
            commander.reasoning_profile,
        ) != expected:
            raise ValueError("Research Commander model route mismatch")
        proposal_kinds = {
            ResearchDecisionKind.PROPOSE_NEW_STRATEGY,
            ResearchDecisionKind.PROPOSE_STRATEGY_REVISION,
            ResearchDecisionKind.PROPOSE_FEATURE_REVISION,
            ResearchDecisionKind.PROPOSE_CALIBRATION_REVISION,
        }
        builder_present = (
            ResearchExecutionRole.CANDIDATE_BUILDER in by_role
        )
        if (self.decision_kind in proposal_kinds) != builder_present:
            raise ValueError(
                "Candidate Builder invocation must match proposal presence"
            )

    def _validate_deep_artifacts(self) -> None:
        kinds = {item.artifact_kind for item in self.artifacts}
        required = {
            ResearchExecutionArtifactKind.WEB_RESEARCH_EVIDENCE,
            ResearchExecutionArtifactKind.RESEARCH_DECISION,
        }
        if not required.issubset(kinds):
            raise ValueError(
                "deep research result requires evidence and decision artifacts"
            )
        proposal_kinds = {
            ResearchDecisionKind.PROPOSE_NEW_STRATEGY,
            ResearchDecisionKind.PROPOSE_STRATEGY_REVISION,
            ResearchDecisionKind.PROPOSE_FEATURE_REVISION,
            ResearchDecisionKind.PROPOSE_CALIBRATION_REVISION,
        }
        proposal_artifacts = {
            ResearchExecutionArtifactKind.ALGORITHM_PROPOSAL,
            ResearchExecutionArtifactKind.CANDIDATE_MANIFEST,
        }
        if self.decision_kind in proposal_kinds:
            if not proposal_artifacts.issubset(kinds):
                raise ValueError(
                    "proposal result requires proposal and Candidate artifacts"
                )
        elif kinds & proposal_artifacts:
            raise ValueError(
                "non-proposal result cannot contain proposal artifacts"
            )


def build_execution_request(
    *,
    execution_lease: ResearchWorkExecutionLeaseV1,
    plan: ResearchSchedulePlanV1,
    receipt: ResearchWorkDispatchReceiptV1,
    commander_selection: CommanderSelectionV1 | None,
) -> ResearchWorkExecutionRequestV1:
    if (
        execution_lease.work_item_id != plan.work_item_id
        or execution_lease.work_item_id != receipt.work_item_id
        or execution_lease.receipt_id != receipt.receipt_id
        or execution_lease.receipt_hash != receipt.receipt_hash
        or execution_lease.work_kind is not plan.work_kind
        or execution_lease.dispatch_attempt_number != receipt.attempt_number
        or execution_lease.config_manifest_hash != plan.config_manifest_hash
    ):
        raise ValueError("research execution lease input binding mismatch")
    payload = {
        "schema_version": EXECUTION_REQUEST_SCHEMA_VERSION,
        "execution_id": execution_lease.execution_id,
        "research_cycle_id": stable_id(
            "scheduled-research-cycle",
            execution_lease.execution_id,
        ),
        "plan": plan,
        "receipt": receipt,
        "commander_selection": commander_selection,
        "config_manifest_hash": plan.config_manifest_hash,
        "automatic_promotion_enabled": False,
        "real_order_routing": False,
    }
    return ResearchWorkExecutionRequestV1.model_validate(
        {**payload, "request_hash": canonical_hash(payload)}
    )


def build_execution_result(
    *,
    request: ResearchWorkExecutionRequestV1,
    artifacts: tuple[ResearchExecutionArtifactV1, ...],
    invocations: tuple[ResearchInvocationAttestationV1, ...],
    decision_kind: ResearchDecisionKind | None,
    completed_at: datetime,
) -> ResearchWorkExecutionResultV1:
    selection = request.commander_selection
    payload = {
        "schema_version": EXECUTION_RESULT_SCHEMA_VERSION,
        "execution_id": request.execution_id,
        "request_hash": request.request_hash,
        "receipt_id": request.receipt.receipt_id,
        "work_item_id": request.plan.work_item_id,
        "work_kind": request.plan.work_kind,
        "dispatch_target": request.receipt.dispatch_target,
        "research_cycle_id": (
            None if selection is None else request.research_cycle_id
        ),
        "commander_selection_id": (
            None if selection is None else selection.selection_id
        ),
        "commander_selection_version": (
            None if selection is None else selection.version
        ),
        "selected_commander": (
            None if selection is None else selection.selected_commander
        ),
        "decision_kind": decision_kind,
        "artifacts": artifacts,
        "invocations": invocations,
        "completed_at": require_aware_utc(completed_at),
        "automatic_promotion_enabled": False,
        "real_order_routing": False,
    }
    result = ResearchWorkExecutionResultV1.model_validate(
        {**payload, "result_hash": canonical_hash(payload)}
    )
    result.assert_bound_to(request)
    return result
