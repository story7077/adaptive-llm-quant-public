from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.research.contracts import (
    HASH_PATTERN,
    IDENTIFIER_PATTERN,
    VERSION_PATTERN,
    CommanderSelectionV1,
    ResearchCommanderKind,
    ResearchDecisionKind,
)
from trading.research.experiment_outcomes import (
    AlgorithmProposalV2,
    ResearchMemorySnapshotV1,
)
from trading.research.meta_controller import ResearchActionPlanV1


class ResearchRequestV2(DomainModel):
    schema_version: Literal["research_request_v2"] = "research_request_v2"
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    research_cycle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    selected_commander: ResearchCommanderKind
    commander_selection_id: str = Field(pattern=IDENTIFIER_PATTERN)
    commander_selection_version: int = Field(ge=1)
    created_at: datetime
    as_of: datetime
    data_available_cutoff: datetime
    expires_at: datetime
    source_snapshot_commit: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    champion_version: str = Field(pattern=VERSION_PATTERN)
    experiment_family: str = Field(pattern=IDENTIFIER_PATTERN)
    champion_manifest: dict[str, JsonValue]
    active_challenger_manifests: list[dict[str, JsonValue]]
    strategy_performance_summary: dict[str, JsonValue]
    failure_case_clusters: list[dict[str, JsonValue]]
    regime_summary: dict[str, JsonValue]
    execution_cost_summary: dict[str, JsonValue]
    capacity_summary: dict[str, JsonValue]
    recent_market_evidence: list[dict[str, JsonValue]]
    recent_web_research: list[dict[str, JsonValue]]
    available_data_catalog: dict[str, JsonValue]
    allowed_change_scope: list[str] = Field(min_length=1)
    forbidden_change_scope: list[str] = Field(min_length=1)
    experiment_budget: dict[str, JsonValue]
    research_memory_snapshot: ResearchMemorySnapshotV1
    research_action_plan: ResearchActionPlanV1
    context_manifest_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "created_at",
        "as_of",
        "data_available_cutoff",
        "expires_at",
        mode="after",
    )
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if not self.created_at <= self.data_available_cutoff <= self.as_of:
            raise ValueError("request time ordering is invalid")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must follow created_at")
        if (
            self.research_action_plan.research_cycle_id
            != self.research_cycle_id
        ):
            raise ValueError("research action plan belongs to another cycle")
        if (
            self.research_action_plan.research_memory_snapshot_hash
            != self.research_memory_snapshot.snapshot_hash
        ):
            raise ValueError("research action plan belongs to another memory snapshot")
        if self.research_memory_snapshot.as_of > self.as_of:
            raise ValueError("research memory snapshot is from the future")
        if (
            self.research_memory_snapshot.data_available_cutoff
            > self.data_available_cutoff
        ):
            raise ValueError("research memory uses data after the request cutoff")
        if self.research_memory_snapshot.created_at > self.created_at:
            raise ValueError("research memory was created after the request")
        if self.research_action_plan.generated_at > self.created_at:
            raise ValueError("research action plan was generated after the request")
        payload = self.model_dump(
            mode="python",
            exclude={"context_manifest_hash"},
        )
        if canonical_hash(payload) != self.context_manifest_hash:
            raise ValueError("context_manifest_hash mismatch")
        return self

    def assert_current_selection(
        self,
        current_selection: CommanderSelectionV1,
    ) -> None:
        if (
            current_selection.selection_id != self.commander_selection_id
            or current_selection.version != self.commander_selection_version
            or current_selection.selected_commander is not self.selected_commander
            or current_selection.created_at > self.created_at
            or current_selection.effective_at > self.created_at
        ):
            raise ValueError("STALE_SELECTION")


class ResearchDecisionV2(DomainModel):
    schema_version: Literal["research_decision_v2"] = "research_decision_v2"
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    research_cycle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    selected_commander: ResearchCommanderKind
    commander_selection_id: str = Field(pattern=IDENTIFIER_PATTERN)
    commander_selection_version: int = Field(ge=1)
    source_snapshot_commit: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    champion_version: str = Field(pattern=VERSION_PATTERN)
    experiment_family: str = Field(pattern=IDENTIFIER_PATTERN)
    context_manifest_hash: str = Field(pattern=HASH_PATTERN)
    request_schema_version: Literal[
        "research_request_v2"
    ] = "research_request_v2"
    request_expires_at: datetime
    decision: ResearchDecisionKind
    rationale: str = Field(min_length=1, max_length=6000)
    proposal: AlgorithmProposalV2 | None = None
    requested_evidence: list[str] = Field(default_factory=list, max_length=100)
    research_memory_snapshot_hash: str = Field(pattern=HASH_PATTERN)
    research_action_plan_hash: str = Field(pattern=HASH_PATTERN)
    created_at: datetime
    output_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("request_expires_at", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        proposal_kinds = {
            ResearchDecisionKind.PROPOSE_NEW_STRATEGY,
            ResearchDecisionKind.PROPOSE_STRATEGY_REVISION,
            ResearchDecisionKind.PROPOSE_FEATURE_REVISION,
            ResearchDecisionKind.PROPOSE_CALIBRATION_REVISION,
        }
        if (self.decision in proposal_kinds) != (self.proposal is not None):
            raise ValueError("proposal presence does not match decision kind")
        if self.decision is ResearchDecisionKind.REQUEST_MORE_EVIDENCE:
            if not self.requested_evidence:
                raise ValueError(
                    "REQUEST_MORE_EVIDENCE requires requested_evidence"
                )
        elif self.requested_evidence:
            raise ValueError(
                "requested_evidence is limited to REQUEST_MORE_EVIDENCE"
            )
        payload = self.model_dump(mode="python", exclude={"output_hash"})
        if canonical_hash(payload) != self.output_hash:
            raise ValueError("ResearchDecisionV2 output hash mismatch")
        return self

    def assert_bound_to_v2(
        self,
        request: ResearchRequestV2,
        *,
        received_at: datetime,
        current_selection: CommanderSelectionV1,
    ) -> None:
        now = require_aware_utc(received_at)
        if not (
            self.request_id == request.request_id
            and self.research_cycle_id == request.research_cycle_id
            and self.selected_commander is request.selected_commander
            and self.commander_selection_id
            == request.commander_selection_id
            and self.commander_selection_version
            == request.commander_selection_version
            and self.source_snapshot_commit == request.source_snapshot_commit
            and self.champion_version == request.champion_version
            and self.experiment_family == request.experiment_family
            and self.context_manifest_hash == request.context_manifest_hash
            and self.request_schema_version == request.schema_version
            and self.request_expires_at == request.expires_at
            and self.research_memory_snapshot_hash
            == request.research_memory_snapshot.snapshot_hash
            and self.research_action_plan_hash
            == request.research_action_plan.plan_hash
        ):
            raise ValueError("ResearchDecisionV2 binding mismatch")
        if now >= request.expires_at:
            raise ValueError("research request expired")
        request.assert_current_selection(current_selection)
        if (
            self.proposal is not None
            and self.proposal.primary_action_kind
            not in request.research_action_plan.permitted_action_kinds()
        ):
            raise ValueError("proposal primary action is outside the action plan")
