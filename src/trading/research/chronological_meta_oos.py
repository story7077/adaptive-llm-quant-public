from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.research.contracts import HASH_PATTERN, IDENTIFIER_PATTERN
from trading.research.experiment_outcomes import ExperimentInformationRole
from trading.research.portfolio_delta_sharpe import (
    PortfolioDeltaSharpeError,
    StationaryBootstrapContractV1,
    calculate_sample_sharpe,
    evaluate_paired_sharpe_returns,
)


class MetaOosPolicyArm(StrEnum):
    STATIC_CHAMPION = "STATIC_CHAMPION"
    FIXED_RECALIBRATION = "FIXED_RECALIBRATION"
    MEMORYLESS_COMMANDER = "MEMORYLESS_COMMANDER"
    ADAPTIVE_META_CONTROLLER = "ADAPTIVE_META_CONTROLLER"


META_OOS_POLICY_ARMS = tuple(MetaOosPolicyArm)


class MetaOosAuditMode(StrEnum):
    RECORDED_PIT_REPLAY = "RECORDED_PIT_REPLAY"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


class MetaOosDecisionKind(StrEnum):
    NO_CHANGE = "NO_CHANGE"
    RUN_ACTION = "RUN_ACTION"


class MetaOosError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class MetaOosCommanderBindingV1(DomainModel):
    schema_version: Literal["meta_oos_commander_binding_v1"] = (
        "meta_oos_commander_binding_v1"
    )
    model_family: str = Field(pattern=IDENTIFIER_PATTERN)
    model_version: str = Field(pattern=IDENTIFIER_PATTERN)
    reasoning_profile: str = Field(pattern=IDENTIFIER_PATTERN)
    prompt_template_hash: str = Field(pattern=HASH_PATTERN)
    request_schema_hash: str = Field(pattern=HASH_PATTERN)
    output_schema_hash: str = Field(pattern=HASH_PATTERN)
    binding_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"binding_hash"})
        if canonical_hash(payload) != self.binding_hash:
            raise ValueError("meta-OOS Commander binding hash mismatch")
        return self


class MetaOosCommanderInvocationV1(DomainModel):
    schema_version: Literal["meta_oos_commander_invocation_v1"] = (
        "meta_oos_commander_invocation_v1"
    )
    commander_binding_hash: str = Field(pattern=HASH_PATTERN)
    model_family: str = Field(pattern=IDENTIFIER_PATTERN)
    model_version: str = Field(pattern=IDENTIFIER_PATTERN)
    reasoning_profile: str = Field(pattern=IDENTIFIER_PATTERN)
    prompt_hash: str = Field(pattern=HASH_PATTERN)
    request_hash: str = Field(pattern=HASH_PATTERN)
    request_schema_hash: str = Field(pattern=HASH_PATTERN)
    output_schema_hash: str = Field(pattern=HASH_PATTERN)
    output_hash: str = Field(pattern=HASH_PATTERN)
    invoked_at: datetime
    invocation_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("invoked_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"invocation_hash"})
        if canonical_hash(payload) != self.invocation_hash:
            raise ValueError("meta-OOS Commander invocation hash mismatch")
        return self


class MetaOosEpochV1(DomainModel):
    schema_version: Literal["meta_oos_epoch_v1"] = "meta_oos_epoch_v1"
    epoch_id: str = Field(pattern=IDENTIFIER_PATTERN)
    decision_at: datetime
    discovery_window_start: datetime
    discovery_window_end: datetime
    maximum_candidate_horizon_sessions: int = Field(ge=1)
    purge_sessions: int = Field(ge=1)
    embargo_sessions: int = Field(ge=1)
    forward_window_start: datetime
    forward_window_end: datetime
    outcome_available_at: datetime
    market_data_manifest_hash: str = Field(pattern=HASH_PATTERN)
    context_key: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_generation_budget: int = Field(ge=0)
    oos_budget: int = Field(ge=0)

    @field_validator(
        "decision_at",
        "discovery_window_start",
        "discovery_window_end",
        "forward_window_start",
        "forward_window_end",
        "outcome_available_at",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_epoch(self) -> Self:
        if not (
            self.discovery_window_start
            < self.discovery_window_end
            < self.decision_at
            < self.forward_window_start
            <= self.forward_window_end
            <= self.outcome_available_at
        ):
            raise ValueError("meta-OOS epoch chronology is invalid")
        if self.purge_sessions < self.maximum_candidate_horizon_sessions:
            raise ValueError("meta-OOS purge is shorter than Candidate horizon")
        return self


class MetaOosEvaluationContractV1(DomainModel):
    schema_version: Literal["meta_oos_evaluation_contract_v1"] = (
        "meta_oos_evaluation_contract_v1"
    )
    contract_version: str = Field(pattern=IDENTIFIER_PATTERN)
    annualization_sessions: int = Field(ge=1, le=366)
    minimum_epochs: int = Field(ge=2)
    maximum_epochs: int = Field(ge=2)
    maximum_candidate_generation_budget_per_epoch: int = Field(ge=1)
    maximum_oos_budget_per_epoch: int = Field(ge=1)
    maximum_outer_audit_uses_per_dataset: int = Field(ge=1)
    reservation_ttl_hours: int = Field(ge=1)
    minimum_adaptive_delta_sharpe_lcb: float
    minimum_research_efficiency: float = Field(ge=0)
    maximum_allowed_drawdown: float = Field(ge=0, le=1)
    tail_quantile: float = Field(gt=0, lt=0.5)
    maximum_absolute_daily_return: float = Field(gt=0)
    prohibit_best_seed_selection: Literal[True] = True
    contract_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "minimum_adaptive_delta_sharpe_lcb",
        "minimum_research_efficiency",
        "maximum_allowed_drawdown",
        "tail_quantile",
        "maximum_absolute_daily_return",
        mode="after",
    )
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("meta-OOS evaluation threshold must be finite")
        return value

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        if self.minimum_epochs > self.maximum_epochs:
            raise ValueError("meta-OOS minimum epochs exceed maximum")
        payload = self.model_dump(mode="python", exclude={"contract_hash"})
        if canonical_hash(payload) != self.contract_hash:
            raise ValueError("meta-OOS evaluation contract hash mismatch")
        return self


class ChronologicalMetaOosPlanV1(DomainModel):
    schema_version: Literal["chronological_meta_oos_plan_v1"] = (
        "chronological_meta_oos_plan_v1"
    )
    plan_id: str = Field(pattern=IDENTIFIER_PATTERN)
    plan_version: str = Field(pattern=IDENTIFIER_PATTERN)
    initial_champion_manifest_hash: str = Field(pattern=HASH_PATTERN)
    epochs: tuple[MetaOosEpochV1, ...] = Field(min_length=2)
    policy_arms: tuple[MetaOosPolicyArm, ...] = Field(
        min_length=4,
        max_length=4,
    )
    policy_adapter_versions: dict[MetaOosPolicyArm, str]
    audit_mode: MetaOosAuditMode
    commander_binding: MetaOosCommanderBindingV1
    meta_controller_version: str = Field(pattern=IDENTIFIER_PATTERN)
    cost_model_hash: str = Field(pattern=HASH_PATTERN)
    execution_model_hash: str = Field(pattern=HASH_PATTERN)
    bootstrap_contract: StationaryBootstrapContractV1
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    outer_audit_dataset_id: str = Field(pattern=IDENTIFIER_PATTERN)
    outer_audit_budget_ordinal: int = Field(ge=1)
    prohibit_best_seed_selection: Literal[True] = True
    created_at: datetime
    real_order_routing: Literal[False] = False
    plan_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if self.policy_arms != META_OOS_POLICY_ARMS:
            raise ValueError("meta-OOS plan must contain the four ordered arms")
        if set(self.policy_adapter_versions) != set(META_OOS_POLICY_ARMS):
            raise ValueError("meta-OOS adapter versions are incomplete")
        if any(not value.strip() for value in self.policy_adapter_versions.values()):
            raise ValueError("meta-OOS adapter version is empty")
        epoch_ids = tuple(item.epoch_id for item in self.epochs)
        if len(epoch_ids) != len(set(epoch_ids)):
            raise ValueError("meta-OOS epoch IDs must be unique")
        if tuple(sorted(self.epochs, key=lambda item: item.decision_at)) != self.epochs:
            raise ValueError("meta-OOS epochs must be ordered")
        for previous, current in zip(self.epochs, self.epochs[1:], strict=False):
            if previous.forward_window_end >= current.discovery_window_start:
                raise ValueError("meta-OOS epoch windows overlap")
            if previous.outcome_available_at > current.decision_at:
                raise ValueError("prior meta-OOS outcome is not mature by next decision")
        if self.created_at > self.epochs[0].decision_at:
            raise ValueError("meta-OOS plan must precede its first decision")
        payload = self.model_dump(mode="python", exclude={"plan_hash"})
        if canonical_hash(payload) != self.plan_hash:
            raise ValueError("chronological meta-OOS plan hash mismatch")
        return self


class MetaOosCandidateAvailabilityV1(DomainModel):
    schema_version: Literal["meta_oos_candidate_availability_v1"] = (
        "meta_oos_candidate_availability_v1"
    )
    candidate_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    first_available_at: datetime
    proposal_created_at: datetime
    source_available_at: tuple[datetime, ...] = Field(min_length=1)
    availability_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "first_available_at",
        "proposal_created_at",
        mode="after",
    )
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("source_available_at", mode="after")
    @classmethod
    def validate_source_times(
        cls,
        value: tuple[datetime, ...],
    ) -> tuple[datetime, ...]:
        return tuple(require_aware_utc(item) for item in value)

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"availability_hash"})
        if canonical_hash(payload) != self.availability_hash:
            raise ValueError("meta-OOS Candidate availability hash mismatch")
        return self

    def is_available(self, decision_at: datetime) -> bool:
        cutoff = require_aware_utc(decision_at)
        return (
            self.first_available_at <= cutoff
            and self.proposal_created_at <= cutoff
            and all(item <= cutoff for item in self.source_available_at)
        )


class MetaOosBudgetV1(DomainModel):
    candidate_generation_budget: int = Field(ge=0)
    oos_budget: int = Field(ge=0)


class MetaOosEpochContextV1(DomainModel):
    schema_version: Literal["meta_oos_epoch_context_v1"] = (
        "meta_oos_epoch_context_v1"
    )
    plan_id: str = Field(pattern=IDENTIFIER_PATTERN)
    plan_hash: str = Field(pattern=HASH_PATTERN)
    epoch_id: str = Field(pattern=IDENTIFIER_PATTERN)
    arm: MetaOosPolicyArm
    decision_at: datetime
    context_key: str = Field(pattern=IDENTIFIER_PATTERN)
    market_data_manifest_hash: str = Field(pattern=HASH_PATTERN)
    available_candidate_ids: tuple[str, ...]
    context_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("decision_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"context_hash"})
        if canonical_hash(payload) != self.context_hash:
            raise ValueError("meta-OOS epoch context hash mismatch")
        return self


class MetaOosLearningOutcomeV1(DomainModel):
    schema_version: Literal["meta_oos_learning_outcome_v1"] = (
        "meta_oos_learning_outcome_v1"
    )
    outcome_id: str = Field(pattern=IDENTIFIER_PATTERN)
    arm: MetaOosPolicyArm
    epoch_id: str = Field(pattern=IDENTIFIER_PATTERN)
    context_key: str = Field(pattern=IDENTIFIER_PATTERN)
    action_kind: str = Field(pattern=IDENTIFIER_PATTERN)
    information_role: ExperimentInformationRole
    available_at: datetime
    matured: bool
    reward: float
    technical_failure: bool
    outcome_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("available_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("reward", mode="after")
    @classmethod
    def validate_reward(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("meta-OOS reward must be finite")
        return value

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"outcome_hash"})
        if canonical_hash(payload) != self.outcome_hash:
            raise ValueError("meta-OOS learning outcome hash mismatch")
        return self


class MetaOosMemorySnapshotV1(DomainModel):
    schema_version: Literal["meta_oos_memory_snapshot_v1"] = (
        "meta_oos_memory_snapshot_v1"
    )
    snapshot_id: str = Field(pattern=IDENTIFIER_PATTERN)
    arm: Literal[MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER]
    as_of: datetime
    outcomes: tuple[MetaOosLearningOutcomeV1, ...]
    outcome_hashes: tuple[str, ...]
    snapshot_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("as_of", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.outcome_hashes != tuple(item.outcome_hash for item in self.outcomes):
            raise ValueError("meta-OOS memory outcome hashes do not match")
        if any(
            not item.matured
            or item.available_at > self.as_of
            or item.information_role
            is not ExperimentInformationRole.LEARNING_FORWARD
            or item.arm is not MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER
            for item in self.outcomes
        ):
            raise ValueError("meta-OOS memory contains protected or future outcome")
        payload = self.model_dump(mode="python", exclude={"snapshot_hash"})
        if canonical_hash(payload) != self.snapshot_hash:
            raise ValueError("meta-OOS memory snapshot hash mismatch")
        return self


class MetaOosPolicyDecisionV1(DomainModel):
    schema_version: Literal["meta_oos_policy_decision_v1"] = (
        "meta_oos_policy_decision_v1"
    )
    decision_id: str = Field(pattern=IDENTIFIER_PATTERN)
    plan_id: str = Field(pattern=IDENTIFIER_PATTERN)
    plan_hash: str = Field(pattern=HASH_PATTERN)
    epoch_id: str = Field(pattern=IDENTIFIER_PATTERN)
    arm: MetaOosPolicyArm
    context_hash: str = Field(pattern=HASH_PATTERN)
    decision_kind: MetaOosDecisionKind
    action_kind: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    candidate_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    research_memory_snapshot_hash: str | None = Field(
        default=None,
        pattern=HASH_PATTERN,
    )
    commander_invocation: MetaOosCommanderInvocationV1 | None = None
    predicted_reward: float | None = None
    created_at: datetime
    decision_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("predicted_reward", mode="after")
    @classmethod
    def validate_prediction(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("meta-OOS prediction must be finite")
        return value

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.decision_kind is MetaOosDecisionKind.NO_CHANGE:
            if self.action_kind is not None or self.candidate_id is not None:
                raise ValueError("NO_CHANGE cannot select an action or Candidate")
        elif self.action_kind is None:
            raise ValueError("RUN_ACTION requires an action")
        payload = self.model_dump(mode="python", exclude={"decision_hash"})
        if canonical_hash(payload) != self.decision_hash:
            raise ValueError("meta-OOS policy decision hash mismatch")
        return self


class ResearchPolicyAdapter(Protocol):
    @property
    def adapter_version(self) -> str: ...

    def plan_research(
        self,
        *,
        epoch_context: MetaOosEpochContextV1,
        research_memory_snapshot: MetaOosMemorySnapshotV1 | None,
        budget: MetaOosBudgetV1,
    ) -> MetaOosPolicyDecisionV1: ...


class StaticChampionPolicyAdapter:
    adapter_version = "static-champion-v1"

    def plan_research(
        self,
        *,
        epoch_context: MetaOosEpochContextV1,
        research_memory_snapshot: MetaOosMemorySnapshotV1 | None,
        budget: MetaOosBudgetV1,
    ) -> MetaOosPolicyDecisionV1:
        del budget
        if research_memory_snapshot is not None:
            raise MetaOosError("STATIC_ARM_RECEIVED_MEMORY")
        return build_meta_oos_policy_decision(
            epoch_context=epoch_context,
            decision_kind=MetaOosDecisionKind.NO_CHANGE,
            action_kind=None,
            candidate_id=None,
            research_memory_snapshot_hash=None,
            commander_invocation=None,
            predicted_reward=None,
        )


class FixedRecalibrationPolicyAdapter:
    adapter_version = "fixed-recalibration-v1"

    def __init__(self, schedule: Mapping[str, str]) -> None:
        self._schedule = dict(schedule)

    def plan_research(
        self,
        *,
        epoch_context: MetaOosEpochContextV1,
        research_memory_snapshot: MetaOosMemorySnapshotV1 | None,
        budget: MetaOosBudgetV1,
    ) -> MetaOosPolicyDecisionV1:
        if research_memory_snapshot is not None:
            raise MetaOosError("FIXED_ARM_RECEIVED_MEMORY")
        action = self._schedule.get(epoch_context.epoch_id)
        if action is None or budget.candidate_generation_budget == 0:
            kind = MetaOosDecisionKind.NO_CHANGE
            action = None
        else:
            kind = MetaOosDecisionKind.RUN_ACTION
        return build_meta_oos_policy_decision(
            epoch_context=epoch_context,
            decision_kind=kind,
            action_kind=action,
            candidate_id=None,
            research_memory_snapshot_hash=None,
            commander_invocation=None,
            predicted_reward=None,
        )


class RecordedMemorylessCommanderAdapter:
    adapter_version = "recorded-memoryless-commander-v1"

    def __init__(
        self,
        decisions: Mapping[str, MetaOosPolicyDecisionV1],
        commander_binding: MetaOosCommanderBindingV1,
    ) -> None:
        self._decisions = dict(decisions)
        self._commander_binding = commander_binding

    def plan_research(
        self,
        *,
        epoch_context: MetaOosEpochContextV1,
        research_memory_snapshot: MetaOosMemorySnapshotV1 | None,
        budget: MetaOosBudgetV1,
    ) -> MetaOosPolicyDecisionV1:
        del budget
        if research_memory_snapshot is not None:
            raise MetaOosError("MEMORYLESS_ARM_RECEIVED_MEMORY")
        decision = self._require_decision(epoch_context)
        if decision.research_memory_snapshot_hash is not None:
            raise MetaOosError("MEMORYLESS_DECISION_BINDS_MEMORY")
        _validate_commander_invocation(
            decision.commander_invocation,
            self._commander_binding,
            decision_at=epoch_context.decision_at,
        )
        return decision

    def _require_decision(
        self,
        context: MetaOosEpochContextV1,
    ) -> MetaOosPolicyDecisionV1:
        decision = self._decisions.get(context.epoch_id)
        if decision is None:
            raise MetaOosError("RECORDED_COMMANDER_DECISION_MISSING")
        _validate_decision_context(decision, context)
        return decision


class RecordedAdaptiveCommanderAdapter:
    adapter_version = "recorded-adaptive-commander-v1"

    def __init__(
        self,
        decisions: Mapping[str, MetaOosPolicyDecisionV1],
        commander_binding: MetaOosCommanderBindingV1,
    ) -> None:
        self._decisions = dict(decisions)
        self._commander_binding = commander_binding

    def plan_research(
        self,
        *,
        epoch_context: MetaOosEpochContextV1,
        research_memory_snapshot: MetaOosMemorySnapshotV1 | None,
        budget: MetaOosBudgetV1,
    ) -> MetaOosPolicyDecisionV1:
        del budget
        if research_memory_snapshot is None:
            raise MetaOosError("ADAPTIVE_ARM_MISSING_MEMORY")
        decision = self._decisions.get(epoch_context.epoch_id)
        if decision is None:
            raise MetaOosError("RECORDED_COMMANDER_DECISION_MISSING")
        _validate_decision_context(decision, epoch_context)
        if (
            decision.research_memory_snapshot_hash
            != research_memory_snapshot.snapshot_hash
        ):
            raise MetaOosError("ADAPTIVE_MEMORY_BINDING_MISMATCH")
        _validate_commander_invocation(
            decision.commander_invocation,
            self._commander_binding,
            decision_at=epoch_context.decision_at,
        )
        return decision


class SyntheticPolicyAdapter:
    adapter_version = "synthetic-context-learning-v1"

    def __init__(self, actions: Sequence[str]) -> None:
        values = tuple(actions)
        if not values or len(values) != len(set(values)):
            raise ValueError("synthetic actions must be non-empty and unique")
        self._actions = values

    def plan_research(
        self,
        *,
        epoch_context: MetaOosEpochContextV1,
        research_memory_snapshot: MetaOosMemorySnapshotV1 | None,
        budget: MetaOosBudgetV1,
    ) -> MetaOosPolicyDecisionV1:
        if budget.candidate_generation_budget == 0:
            return build_meta_oos_policy_decision(
                epoch_context=epoch_context,
                decision_kind=MetaOosDecisionKind.NO_CHANGE,
                action_kind=None,
                candidate_id=None,
                research_memory_snapshot_hash=(
                    None
                    if research_memory_snapshot is None
                    else research_memory_snapshot.snapshot_hash
                ),
                commander_invocation=None,
                predicted_reward=None,
            )
        relevant = (
            ()
            if research_memory_snapshot is None
            else tuple(
                item
                for item in research_memory_snapshot.outcomes
                if item.context_key == epoch_context.context_key
                and not item.technical_failure
            )
        )
        by_action: dict[str, list[float]] = defaultdict(list)
        for item in relevant:
            by_action[item.action_kind].append(item.reward)
        unseen = tuple(action for action in self._actions if action not in by_action)
        if unseen:
            action = unseen[0]
            predicted = None
        else:
            means = {
                action: sum(by_action[action]) / len(by_action[action])
                for action in self._actions
            }
            action = max(self._actions, key=lambda item: (means[item], -self._actions.index(item)))
            predicted = means[action]
        return build_meta_oos_policy_decision(
            epoch_context=epoch_context,
            decision_kind=MetaOosDecisionKind.RUN_ACTION,
            action_kind=action,
            candidate_id=None,
            research_memory_snapshot_hash=(
                None
                if research_memory_snapshot is None
                else research_memory_snapshot.snapshot_hash
            ),
            commander_invocation=None,
            predicted_reward=predicted,
        )


@dataclass(frozen=True, slots=True)
class MetaOosPrivateSessionObservation:
    session_key: str
    occurred_at: datetime
    available_at: datetime
    net_return: float
    turnover: float
    regime: str


@dataclass(frozen=True, slots=True)
class MetaOosPrivateEpochEvaluation:
    epoch_id: str
    observations: tuple[MetaOosPrivateSessionObservation, ...]
    candidate_count: int
    oos_use_count: int
    promotion_eligible_count: int
    technical_failure: bool
    information_role: ExperimentInformationRole
    learning_reward: float
    candidate_artifact_hash: str | None


class MetaOosEnvironment(Protocol):
    def execute_epoch(
        self,
        *,
        plan: ChronologicalMetaOosPlanV1,
        epoch: MetaOosEpochV1,
        arm: MetaOosPolicyArm,
        decision: MetaOosPolicyDecisionV1,
    ) -> MetaOosPrivateEpochEvaluation: ...


class DeterministicSyntheticMetaOosEnvironment:
    """Deterministic CI fixture; its output is not performance evidence."""

    def __init__(
        self,
        *,
        context_action_edge: Mapping[str, str],
        sessions_per_epoch: int = 12,
        correct_action_edge: float = 0.0015,
        wrong_action_edge: float = -0.0005,
        role_by_epoch: Mapping[str, ExperimentInformationRole] | None = None,
    ) -> None:
        if sessions_per_epoch < 2:
            raise ValueError("synthetic meta-OOS needs at least two sessions")
        self._context_action_edge = dict(context_action_edge)
        self._sessions_per_epoch = sessions_per_epoch
        self._correct_action_edge = correct_action_edge
        self._wrong_action_edge = wrong_action_edge
        self._role_by_epoch = dict(role_by_epoch or {})

    def execute_epoch(
        self,
        *,
        plan: ChronologicalMetaOosPlanV1,
        epoch: MetaOosEpochV1,
        arm: MetaOosPolicyArm,
        decision: MetaOosPolicyDecisionV1,
    ) -> MetaOosPrivateEpochEvaluation:
        del arm
        best_action = self._context_action_edge.get(epoch.context_key)
        if decision.action_kind is None or best_action is None:
            edge = 0.0
        elif decision.action_kind == best_action:
            edge = self._correct_action_edge
        else:
            edge = self._wrong_action_edge
        observations: list[MetaOosPrivateSessionObservation] = []
        for index in range(self._sessions_per_epoch):
            occurred_at = epoch.forward_window_start + timedelta(days=index)
            if occurred_at > epoch.forward_window_end:
                raise MetaOosError("SYNTHETIC_FORWARD_WINDOW_TOO_SHORT")
            seed = int(
                canonical_hash(
                    {
                        "plan_hash": plan.plan_hash,
                        "epoch_id": epoch.epoch_id,
                        "session_index": index,
                    }
                )[:16],
                16,
            )
            common_return = random.Random(seed).gauss(0.0002, 0.008)
            observations.append(
                MetaOosPrivateSessionObservation(
                    session_key=f"{epoch.epoch_id}:{index:04d}",
                    occurred_at=occurred_at,
                    available_at=occurred_at,
                    net_return=common_return + edge,
                    turnover=0.005 if decision.action_kind is None else 0.02,
                    regime=epoch.context_key,
                )
            )
        role = self._role_by_epoch.get(
            epoch.epoch_id,
            ExperimentInformationRole.LEARNING_FORWARD,
        )
        return MetaOosPrivateEpochEvaluation(
            epoch_id=epoch.epoch_id,
            observations=tuple(observations),
            candidate_count=0 if decision.action_kind is None else 1,
            oos_use_count=0 if decision.action_kind is None else 1,
            promotion_eligible_count=(
                1
                if decision.action_kind is not None
                and decision.action_kind == best_action
                else 0
            ),
            technical_failure=False,
            information_role=role,
            learning_reward=edge,
            candidate_artifact_hash=(
                None
                if decision.action_kind is None
                else canonical_hash(
                    {
                        "plan_hash": plan.plan_hash,
                        "epoch_id": epoch.epoch_id,
                        "action_kind": decision.action_kind,
                    }
                )
            ),
        )


class MetaOosOuterAuditReservationV1(DomainModel):
    schema_version: Literal["meta_oos_outer_audit_reservation_v1"] = (
        "meta_oos_outer_audit_reservation_v1"
    )
    reservation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    plan_id: str = Field(pattern=IDENTIFIER_PATTERN)
    plan_hash: str = Field(pattern=HASH_PATTERN)
    outer_audit_dataset_id: str = Field(pattern=IDENTIFIER_PATTERN)
    outer_audit_budget_ordinal: int = Field(ge=1)
    idempotency_key: str = Field(pattern=IDENTIFIER_PATTERN)
    created_at: datetime
    expires_at: datetime
    real_order_routing: Literal[False] = False
    reservation_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", "expires_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_reservation(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("meta-OOS reservation must expire after creation")
        payload = self.model_dump(mode="python", exclude={"reservation_hash"})
        if canonical_hash(payload) != self.reservation_hash:
            raise ValueError("meta-OOS outer reservation hash mismatch")
        return self


class MetaOosEpochArmAuditRecordV1(DomainModel):
    schema_version: Literal["meta_oos_epoch_arm_audit_record_v1"] = (
        "meta_oos_epoch_arm_audit_record_v1"
    )
    record_id: str = Field(pattern=IDENTIFIER_PATTERN)
    plan_id: str = Field(pattern=IDENTIFIER_PATTERN)
    plan_hash: str = Field(pattern=HASH_PATTERN)
    epoch_id: str = Field(pattern=IDENTIFIER_PATTERN)
    arm: MetaOosPolicyArm
    decision_hash: str = Field(pattern=HASH_PATTERN)
    memory_snapshot_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    candidate_artifact_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    private_outcome_hash: str = Field(pattern=HASH_PATTERN)
    session_count: int = Field(ge=2)
    experiment_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    oos_use_count: int = Field(ge=0)
    positive_matured_outcome_count: int = Field(ge=0)
    technical_failure_count: int = Field(ge=0)
    promotion_eligible_count: int = Field(ge=0)
    outcome_available_at: datetime
    created_at: datetime
    real_order_routing: Literal[False] = False
    record_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("outcome_available_at", "created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if self.created_at < self.outcome_available_at:
            raise ValueError("meta-OOS audit record predates outcome")
        payload = self.model_dump(mode="python", exclude={"record_hash"})
        if canonical_hash(payload) != self.record_hash:
            raise ValueError("meta-OOS epoch audit record hash mismatch")
        return self


class MetaOosGroupAggregateV1(DomainModel):
    observations: int = Field(ge=1)
    mean_net_return: float
    positive_fraction: float = Field(ge=0, le=1)

    @field_validator("mean_net_return", "positive_fraction", mode="after")
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("meta-OOS group aggregate must be finite")
        return value


class MetaOosPredictionCalibrationV1(DomainModel):
    prediction_count: int = Field(ge=0)
    mean_error: float | None
    mean_absolute_error: float | None

    @field_validator("mean_error", "mean_absolute_error", mode="after")
    @classmethod
    def validate_optional_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("meta-OOS calibration metric must be finite")
        return value


class MetaOosArmAggregateV1(DomainModel):
    schema_version: Literal["meta_oos_arm_aggregate_v1"] = (
        "meta_oos_arm_aggregate_v1"
    )
    arm: MetaOosPolicyArm
    net_sequence_return: float
    annualized_volatility: float = Field(ge=0)
    portfolio_sharpe: float
    maximum_drawdown: float = Field(ge=0, le=1)
    tail_loss: float = Field(ge=0)
    annualized_turnover: float = Field(ge=0)
    experiment_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    oos_use_count: int = Field(ge=0)
    positive_matured_outcome_count: int = Field(ge=0)
    technical_failure_count: int = Field(ge=0)
    promotion_eligible_count: int = Field(ge=0)
    regime_results: dict[str, MetaOosGroupAggregateV1]
    action_results: dict[str, MetaOosGroupAggregateV1]
    prediction_calibration: MetaOosPredictionCalibrationV1
    arm_result_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "net_sequence_return",
        "annualized_volatility",
        "portfolio_sharpe",
        "maximum_drawdown",
        "tail_loss",
        "annualized_turnover",
        mode="after",
    )
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("meta-OOS arm metric must be finite")
        return value

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"arm_result_hash"})
        if canonical_hash(payload) != self.arm_result_hash:
            raise ValueError("meta-OOS arm result hash mismatch")
        return self


class MetaOosPairedComparisonV1(DomainModel):
    schema_version: Literal["meta_oos_paired_comparison_v1"] = (
        "meta_oos_paired_comparison_v1"
    )
    candidate_arm: MetaOosPolicyArm
    baseline_arm: MetaOosPolicyArm
    common_sessions: int = Field(ge=2)
    delta_sharpe_point: float
    delta_sharpe_lcb: float
    delta_sharpe_ucb: float
    comparison_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "delta_sharpe_point",
        "delta_sharpe_lcb",
        "delta_sharpe_ucb",
        mode="after",
    )
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("meta-OOS comparison metric must be finite")
        return value

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        if self.candidate_arm is self.baseline_arm:
            raise ValueError("meta-OOS comparison arms must differ")
        if self.delta_sharpe_lcb > self.delta_sharpe_ucb:
            raise ValueError("meta-OOS comparison interval is inverted")
        payload = self.model_dump(mode="python", exclude={"comparison_hash"})
        if canonical_hash(payload) != self.comparison_hash:
            raise ValueError("meta-OOS comparison hash mismatch")
        return self


class ChronologicalMetaOosResultV1(DomainModel):
    schema_version: Literal["chronological_meta_oos_result_v1"] = (
        "chronological_meta_oos_result_v1"
    )
    result_id: str = Field(pattern=IDENTIFIER_PATTERN)
    plan_id: str = Field(pattern=IDENTIFIER_PATTERN)
    plan_hash: str = Field(pattern=HASH_PATTERN)
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    outer_audit_reservation_hash: str = Field(pattern=HASH_PATTERN)
    outer_audit_dataset_id: str = Field(pattern=IDENTIFIER_PATTERN)
    outer_audit_budget_ordinal: int = Field(ge=1)
    audit_record_hashes: tuple[str, ...] = Field(min_length=8)
    arm_results: tuple[MetaOosArmAggregateV1, ...] = Field(
        min_length=4,
        max_length=4,
    )
    paired_comparisons: tuple[MetaOosPairedComparisonV1, ...] = Field(
        min_length=9,
        max_length=9,
    )
    adaptive_research_efficiency: float = Field(ge=0)
    experiments_per_positive_delta_sharpe_lcb: float | None
    oos_uses_per_positive_delta_sharpe_lcb: float | None
    no_pit_or_binding_violation: Literal[True] = True
    adaptive_system_pass: bool
    reason_codes: tuple[str, ...]
    evaluated_at: datetime
    real_order_routing: Literal[False] = False
    result_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("evaluated_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator(
        "adaptive_research_efficiency",
        "experiments_per_positive_delta_sharpe_lcb",
        "oos_uses_per_positive_delta_sharpe_lcb",
        mode="after",
    )
    @classmethod
    def validate_optional_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("meta-OOS efficiency metric must be finite")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if tuple(item.arm for item in self.arm_results) != META_OOS_POLICY_ARMS:
            raise ValueError("meta-OOS result arm order mismatch")
        expected_pairs = {
            (candidate, baseline)
            for candidate in META_OOS_POLICY_ARMS
            for baseline in (
                MetaOosPolicyArm.STATIC_CHAMPION,
                MetaOosPolicyArm.FIXED_RECALIBRATION,
                MetaOosPolicyArm.MEMORYLESS_COMMANDER,
            )
            if candidate is not baseline
        }
        actual_pairs = {
            (item.candidate_arm, item.baseline_arm)
            for item in self.paired_comparisons
        }
        if actual_pairs != expected_pairs:
            raise ValueError("meta-OOS result comparison matrix is incomplete")
        if self.adaptive_system_pass != (not self.reason_codes):
            raise ValueError("meta-OOS verdict and reason codes disagree")
        payload = self.model_dump(mode="python", exclude={"result_hash"})
        if canonical_hash(payload) != self.result_hash:
            raise ValueError("chronological meta-OOS result hash mismatch")
        return self


@dataclass(frozen=True, slots=True)
class ChronologicalMetaOosRunV1:
    result: ChronologicalMetaOosResultV1
    audit_records: tuple[MetaOosEpochArmAuditRecordV1, ...]


def build_meta_oos_commander_binding(
    *,
    model_family: str,
    model_version: str,
    reasoning_profile: str,
    prompt_template_hash: str,
    request_schema_hash: str,
    output_schema_hash: str,
) -> MetaOosCommanderBindingV1:
    payload = {
        "schema_version": "meta_oos_commander_binding_v1",
        "model_family": model_family,
        "model_version": model_version,
        "reasoning_profile": reasoning_profile,
        "prompt_template_hash": prompt_template_hash,
        "request_schema_hash": request_schema_hash,
        "output_schema_hash": output_schema_hash,
    }
    return MetaOosCommanderBindingV1.model_validate(
        {**payload, "binding_hash": canonical_hash(payload)}
    )


def build_meta_oos_evaluation_contract(
    *,
    contract_version: str,
    annualization_sessions: int,
    minimum_epochs: int,
    maximum_epochs: int,
    maximum_candidate_generation_budget_per_epoch: int,
    maximum_oos_budget_per_epoch: int,
    maximum_outer_audit_uses_per_dataset: int,
    reservation_ttl_hours: int,
    minimum_adaptive_delta_sharpe_lcb: float,
    minimum_research_efficiency: float,
    maximum_allowed_drawdown: float,
    tail_quantile: float,
    maximum_absolute_daily_return: float,
) -> MetaOosEvaluationContractV1:
    payload = {
        "schema_version": "meta_oos_evaluation_contract_v1",
        "contract_version": contract_version,
        "annualization_sessions": annualization_sessions,
        "minimum_epochs": minimum_epochs,
        "maximum_epochs": maximum_epochs,
        "maximum_candidate_generation_budget_per_epoch": (
            maximum_candidate_generation_budget_per_epoch
        ),
        "maximum_oos_budget_per_epoch": maximum_oos_budget_per_epoch,
        "maximum_outer_audit_uses_per_dataset": (
            maximum_outer_audit_uses_per_dataset
        ),
        "reservation_ttl_hours": reservation_ttl_hours,
        "minimum_adaptive_delta_sharpe_lcb": float(
            minimum_adaptive_delta_sharpe_lcb
        ),
        "minimum_research_efficiency": float(minimum_research_efficiency),
        "maximum_allowed_drawdown": float(maximum_allowed_drawdown),
        "tail_quantile": float(tail_quantile),
        "maximum_absolute_daily_return": float(
            maximum_absolute_daily_return
        ),
        "prohibit_best_seed_selection": True,
    }
    return MetaOosEvaluationContractV1.model_validate(
        {**payload, "contract_hash": canonical_hash(payload)}
    )


def build_meta_oos_commander_invocation(
    *,
    commander_binding: MetaOosCommanderBindingV1,
    prompt_hash: str,
    request_hash: str,
    output_hash: str,
    invoked_at: datetime,
) -> MetaOosCommanderInvocationV1:
    payload = {
        "schema_version": "meta_oos_commander_invocation_v1",
        "commander_binding_hash": commander_binding.binding_hash,
        "model_family": commander_binding.model_family,
        "model_version": commander_binding.model_version,
        "reasoning_profile": commander_binding.reasoning_profile,
        "prompt_hash": prompt_hash,
        "request_hash": request_hash,
        "request_schema_hash": commander_binding.request_schema_hash,
        "output_schema_hash": commander_binding.output_schema_hash,
        "output_hash": output_hash,
        "invoked_at": require_aware_utc(invoked_at),
    }
    return MetaOosCommanderInvocationV1.model_validate(
        {**payload, "invocation_hash": canonical_hash(payload)}
    )


def build_chronological_meta_oos_plan(
    *,
    plan_id: str,
    plan_version: str,
    initial_champion_manifest_hash: str,
    epochs: Sequence[MetaOosEpochV1],
    policy_adapter_versions: Mapping[MetaOosPolicyArm, str],
    audit_mode: MetaOosAuditMode,
    commander_binding: MetaOosCommanderBindingV1,
    meta_controller_version: str,
    cost_model_hash: str,
    execution_model_hash: str,
    bootstrap_contract: StationaryBootstrapContractV1,
    evaluation_contract_hash: str,
    outer_audit_dataset_id: str,
    outer_audit_budget_ordinal: int,
    created_at: datetime,
) -> ChronologicalMetaOosPlanV1:
    payload = {
        "schema_version": "chronological_meta_oos_plan_v1",
        "plan_id": plan_id,
        "plan_version": plan_version,
        "initial_champion_manifest_hash": initial_champion_manifest_hash,
        "epochs": tuple(epochs),
        "policy_arms": META_OOS_POLICY_ARMS,
        "policy_adapter_versions": dict(policy_adapter_versions),
        "audit_mode": audit_mode,
        "commander_binding": commander_binding,
        "meta_controller_version": meta_controller_version,
        "cost_model_hash": cost_model_hash,
        "execution_model_hash": execution_model_hash,
        "bootstrap_contract": bootstrap_contract,
        "evaluation_contract_hash": evaluation_contract_hash,
        "outer_audit_dataset_id": outer_audit_dataset_id,
        "outer_audit_budget_ordinal": outer_audit_budget_ordinal,
        "prohibit_best_seed_selection": True,
        "created_at": require_aware_utc(created_at),
        "real_order_routing": False,
    }
    return ChronologicalMetaOosPlanV1.model_validate(
        {**payload, "plan_hash": canonical_hash(payload)}
    )


def build_meta_oos_policy_decision(
    *,
    epoch_context: MetaOosEpochContextV1,
    decision_kind: MetaOosDecisionKind,
    action_kind: str | None,
    candidate_id: str | None,
    research_memory_snapshot_hash: str | None,
    commander_invocation: MetaOosCommanderInvocationV1 | None,
    predicted_reward: float | None,
) -> MetaOosPolicyDecisionV1:
    decision_id = stable_id(
        "meta-oos-decision",
        epoch_context.plan_hash,
        epoch_context.epoch_id,
        epoch_context.arm,
    )
    payload = {
        "schema_version": "meta_oos_policy_decision_v1",
        "decision_id": decision_id,
        "plan_id": epoch_context.plan_id,
        "plan_hash": epoch_context.plan_hash,
        "epoch_id": epoch_context.epoch_id,
        "arm": epoch_context.arm,
        "context_hash": epoch_context.context_hash,
        "decision_kind": decision_kind,
        "action_kind": action_kind,
        "candidate_id": candidate_id,
        "research_memory_snapshot_hash": research_memory_snapshot_hash,
        "commander_invocation": commander_invocation,
        "predicted_reward": predicted_reward,
        "created_at": epoch_context.decision_at,
    }
    return MetaOosPolicyDecisionV1.model_validate(
        {**payload, "decision_hash": canonical_hash(payload)}
    )


def build_meta_oos_outer_audit_reservation(
    *,
    plan: ChronologicalMetaOosPlanV1,
    idempotency_key: str,
    created_at: datetime,
    expires_at: datetime,
) -> MetaOosOuterAuditReservationV1:
    timestamp = require_aware_utc(created_at)
    reservation_id = stable_id(
        "meta-oos-outer-reservation",
        plan.plan_hash,
        plan.outer_audit_dataset_id,
        plan.outer_audit_budget_ordinal,
        idempotency_key,
    )
    payload = {
        "schema_version": "meta_oos_outer_audit_reservation_v1",
        "reservation_id": reservation_id,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "outer_audit_dataset_id": plan.outer_audit_dataset_id,
        "outer_audit_budget_ordinal": plan.outer_audit_budget_ordinal,
        "idempotency_key": idempotency_key,
        "created_at": timestamp,
        "expires_at": require_aware_utc(expires_at),
        "real_order_routing": False,
    }
    return MetaOosOuterAuditReservationV1.model_validate(
        {**payload, "reservation_hash": canonical_hash(payload)}
    )


def build_meta_oos_memory_snapshot(
    *,
    outcomes: Sequence[MetaOosLearningOutcomeV1],
    as_of: datetime,
) -> MetaOosMemorySnapshotV1:
    cutoff = require_aware_utc(as_of)
    eligible = tuple(
        sorted(
            (
                item
                for item in outcomes
                if item.arm is MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER
                and item.matured
                and item.available_at <= cutoff
                and item.information_role
                is ExperimentInformationRole.LEARNING_FORWARD
            ),
            key=lambda item: (
                item.available_at,
                item.epoch_id,
                item.outcome_id,
            ),
        )
    )
    snapshot_id = stable_id(
        "meta-oos-memory",
        cutoff,
        tuple(item.outcome_hash for item in eligible),
    )
    payload = {
        "schema_version": "meta_oos_memory_snapshot_v1",
        "snapshot_id": snapshot_id,
        "arm": MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER,
        "as_of": cutoff,
        "outcomes": eligible,
        "outcome_hashes": tuple(item.outcome_hash for item in eligible),
    }
    return MetaOosMemorySnapshotV1.model_validate(
        {**payload, "snapshot_hash": canonical_hash(payload)}
    )


def run_chronological_meta_oos(
    *,
    plan: ChronologicalMetaOosPlanV1,
    evaluation_contract: MetaOosEvaluationContractV1,
    adapters: Mapping[MetaOosPolicyArm, ResearchPolicyAdapter],
    environment: MetaOosEnvironment,
    outer_audit_reservation_hash: str,
    candidate_catalog: Mapping[str, MetaOosCandidateAvailabilityV1] | None = None,
    evaluated_at: datetime,
) -> ChronologicalMetaOosRunV1:
    timestamp = require_aware_utc(evaluated_at)
    verify_chronological_meta_oos_plan(
        plan=plan,
        evaluation_contract=evaluation_contract,
    )
    if set(adapters) != set(META_OOS_POLICY_ARMS):
        raise MetaOosError("META_OOS_POLICY_ADAPTER_SET_MISMATCH")
    if len({id(adapters[arm]) for arm in META_OOS_POLICY_ARMS}) != len(
        META_OOS_POLICY_ARMS
    ):
        raise MetaOosError("META_OOS_POLICY_ADAPTER_STATE_SHARED")
    for arm, adapter in adapters.items():
        if adapter.adapter_version != plan.policy_adapter_versions[arm]:
            raise MetaOosError("META_OOS_POLICY_ADAPTER_VERSION_MISMATCH")
    if timestamp < plan.epochs[-1].outcome_available_at:
        raise MetaOosError("META_OOS_RESULT_PRECEDES_FINAL_OUTCOME")
    catalog = dict(candidate_catalog or {})
    outcomes: dict[MetaOosPolicyArm, list[MetaOosLearningOutcomeV1]] = {
        arm: [] for arm in META_OOS_POLICY_ARMS
    }
    private_sessions: dict[
        MetaOosPolicyArm,
        list[tuple[MetaOosPrivateSessionObservation, str, float | None]],
    ] = {arm: [] for arm in META_OOS_POLICY_ARMS}
    prediction_pairs: dict[
        MetaOosPolicyArm,
        list[tuple[float, float]],
    ] = {arm: [] for arm in META_OOS_POLICY_ARMS}
    audit_records: list[MetaOosEpochArmAuditRecordV1] = []

    for epoch in plan.epochs:
        epoch_evaluations: dict[
            MetaOosPolicyArm,
            tuple[
                MetaOosPolicyDecisionV1,
                MetaOosMemorySnapshotV1 | None,
                MetaOosPrivateEpochEvaluation,
            ],
        ] = {}
        available_candidates = tuple(
            sorted(
                candidate_id
                for candidate_id, candidate in catalog.items()
                if candidate.is_available(epoch.decision_at)
            )
        )
        for arm in META_OOS_POLICY_ARMS:
            context_payload = {
                "schema_version": "meta_oos_epoch_context_v1",
                "plan_id": plan.plan_id,
                "plan_hash": plan.plan_hash,
                "epoch_id": epoch.epoch_id,
                "arm": arm,
                "decision_at": epoch.decision_at,
                "context_key": epoch.context_key,
                "market_data_manifest_hash": (
                    epoch.market_data_manifest_hash
                ),
                "available_candidate_ids": available_candidates,
            }
            context = MetaOosEpochContextV1.model_validate(
                {
                    **context_payload,
                    "context_hash": canonical_hash(context_payload),
                }
            )
            memory = (
                build_meta_oos_memory_snapshot(
                    outcomes=outcomes[arm],
                    as_of=epoch.decision_at,
                )
                if arm is MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER
                else None
            )
            decision = adapters[arm].plan_research(
                epoch_context=context,
                research_memory_snapshot=memory,
                budget=MetaOosBudgetV1(
                    candidate_generation_budget=(
                        epoch.candidate_generation_budget
                    ),
                    oos_budget=epoch.oos_budget,
                ),
            )
            _validate_decision_context(decision, context)
            _validate_decision_memory(decision, arm, memory)
            _validate_decision_commander(plan, decision)
            _validate_candidate_selection(
                decision,
                catalog,
                epoch.decision_at,
            )
            private = environment.execute_epoch(
                plan=plan,
                epoch=epoch,
                arm=arm,
                decision=decision,
            )
            _validate_private_epoch(
                private,
                epoch=epoch,
                evaluation_contract=evaluation_contract,
            )
            if (
                private.candidate_count
                > epoch.candidate_generation_budget
                or private.oos_use_count > epoch.oos_budget
            ):
                raise MetaOosError("META_OOS_EPOCH_BUDGET_EXCEEDED")
            epoch_evaluations[arm] = (decision, memory, private)

        reference_keys = tuple(
            item.session_key
            for item in epoch_evaluations[
                MetaOosPolicyArm.STATIC_CHAMPION
            ][2].observations
        )
        for arm in META_OOS_POLICY_ARMS:
            decision, memory, private = epoch_evaluations[arm]
            if tuple(item.session_key for item in private.observations) != reference_keys:
                raise MetaOosError("META_OOS_ARM_SESSION_MISMATCH")
            action = decision.action_kind or "NO_CHANGE"
            for observation in private.observations:
                private_sessions[arm].append(
                    (observation, action, decision.predicted_reward)
                )
            learning = _build_learning_outcome(
                arm=arm,
                epoch=epoch,
                decision=decision,
                private=private,
            )
            outcomes[arm].append(learning)
            if decision.predicted_reward is not None:
                prediction_pairs[arm].append(
                    (decision.predicted_reward, learning.reward)
                )
            record = _build_epoch_audit_record(
                plan=plan,
                epoch=epoch,
                arm=arm,
                decision=decision,
                memory=memory,
                private=private,
            )
            audit_records.append(record)

    reference_session_keys = tuple(
        item[0].session_key
        for item in private_sessions[MetaOosPolicyArm.STATIC_CHAMPION]
    )
    for arm in META_OOS_POLICY_ARMS:
        if tuple(item[0].session_key for item in private_sessions[arm]) != (
            reference_session_keys
        ):
            raise MetaOosError("META_OOS_GLOBAL_SESSION_MISMATCH")

    arm_results = tuple(
        _build_arm_aggregate(
            arm=arm,
            sessions=private_sessions[arm],
            audit_records=tuple(
                item for item in audit_records if item.arm is arm
            ),
            prediction_pairs=prediction_pairs[arm],
            evaluation_contract=evaluation_contract,
        )
        for arm in META_OOS_POLICY_ARMS
    )
    paired = _build_pairwise_comparisons(
        plan=plan,
        private_sessions=private_sessions,
        evaluation_contract=evaluation_contract,
    )
    adaptive = next(
        item
        for item in arm_results
        if item.arm is MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER
    )
    adaptive_comparisons = tuple(
        item
        for item in paired
        if item.candidate_arm
        is MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER
    )
    positive_lcb_count = sum(
        item.delta_sharpe_lcb
        > evaluation_contract.minimum_adaptive_delta_sharpe_lcb
        for item in adaptive_comparisons
    )
    efficiency = positive_lcb_count / max(1, adaptive.experiment_count)
    reasons = _adaptive_verdict_reason_codes(
        adaptive=adaptive,
        adaptive_comparisons=adaptive_comparisons,
        adaptive_research_efficiency=efficiency,
        evaluation_contract=evaluation_contract,
    )
    result_id = stable_id(
        "chronological-meta-oos-result",
        plan.plan_hash,
        outer_audit_reservation_hash,
        tuple(item.record_hash for item in audit_records),
    )
    payload = {
        "schema_version": "chronological_meta_oos_result_v1",
        "result_id": result_id,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "evaluation_contract_hash": evaluation_contract.contract_hash,
        "outer_audit_reservation_hash": outer_audit_reservation_hash,
        "outer_audit_dataset_id": plan.outer_audit_dataset_id,
        "outer_audit_budget_ordinal": plan.outer_audit_budget_ordinal,
        "audit_record_hashes": tuple(
            item.record_hash for item in audit_records
        ),
        "arm_results": arm_results,
        "paired_comparisons": paired,
        "adaptive_research_efficiency": efficiency,
        "experiments_per_positive_delta_sharpe_lcb": (
            None
            if positive_lcb_count == 0
            else adaptive.experiment_count / positive_lcb_count
        ),
        "oos_uses_per_positive_delta_sharpe_lcb": (
            None
            if positive_lcb_count == 0
            else adaptive.oos_use_count / positive_lcb_count
        ),
        "no_pit_or_binding_violation": True,
        "adaptive_system_pass": not reasons,
        "reason_codes": tuple(reasons),
        "evaluated_at": timestamp,
        "real_order_routing": False,
    }
    result = ChronologicalMetaOosResultV1.model_validate(
        {**payload, "result_hash": canonical_hash(payload)}
    )
    return ChronologicalMetaOosRunV1(
        result=result,
        audit_records=tuple(audit_records),
    )


def verify_chronological_meta_oos_result(
    *,
    plan: ChronologicalMetaOosPlanV1,
    evaluation_contract: MetaOosEvaluationContractV1,
    result: ChronologicalMetaOosResultV1,
) -> None:
    verify_chronological_meta_oos_plan(
        plan=plan,
        evaluation_contract=evaluation_contract,
    )
    if (
        result.plan_id != plan.plan_id
        or result.plan_hash != plan.plan_hash
        or result.evaluation_contract_hash
        != evaluation_contract.contract_hash
        or result.outer_audit_dataset_id != plan.outer_audit_dataset_id
        or result.outer_audit_budget_ordinal
        != plan.outer_audit_budget_ordinal
        or result.real_order_routing
    ):
        raise MetaOosError("META_OOS_RESULT_BINDING_MISMATCH")
    if (
        result.evaluated_at < plan.epochs[-1].outcome_available_at
        or len(result.audit_record_hashes)
        != len(plan.epochs) * len(META_OOS_POLICY_ARMS)
        or len(set(result.audit_record_hashes))
        != len(result.audit_record_hashes)
    ):
        raise MetaOosError("META_OOS_RESULT_AUDIT_SET_MISMATCH")
    adaptive = next(
        item
        for item in result.arm_results
        if item.arm is MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER
    )
    adaptive_comparisons = tuple(
        item
        for item in result.paired_comparisons
        if item.candidate_arm
        is MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER
    )
    positive_lcb_count = sum(
        item.delta_sharpe_lcb
        > evaluation_contract.minimum_adaptive_delta_sharpe_lcb
        for item in adaptive_comparisons
    )
    expected_efficiency = positive_lcb_count / max(
        1,
        adaptive.experiment_count,
    )
    expected_experiments_per_positive = (
        None
        if positive_lcb_count == 0
        else adaptive.experiment_count / positive_lcb_count
    )
    expected_oos_per_positive = (
        None
        if positive_lcb_count == 0
        else adaptive.oos_use_count / positive_lcb_count
    )
    reasons = _adaptive_verdict_reason_codes(
        adaptive=adaptive,
        adaptive_comparisons=adaptive_comparisons,
        adaptive_research_efficiency=expected_efficiency,
        evaluation_contract=evaluation_contract,
    )
    if (
        result.adaptive_research_efficiency != expected_efficiency
        or result.experiments_per_positive_delta_sharpe_lcb
        != expected_experiments_per_positive
        or result.oos_uses_per_positive_delta_sharpe_lcb
        != expected_oos_per_positive
        or result.reason_codes != reasons
        or result.adaptive_system_pass != (not reasons)
    ):
        raise MetaOosError("META_OOS_RESULT_VERDICT_MISMATCH")


def verify_chronological_meta_oos_plan(
    *,
    plan: ChronologicalMetaOosPlanV1,
    evaluation_contract: MetaOosEvaluationContractV1,
) -> None:
    if plan.evaluation_contract_hash != evaluation_contract.contract_hash:
        raise MetaOosError("META_OOS_EVALUATION_CONTRACT_MISMATCH")
    if len(plan.epochs) < evaluation_contract.minimum_epochs:
        raise MetaOosError("META_OOS_INSUFFICIENT_EPOCHS")
    if len(plan.epochs) > evaluation_contract.maximum_epochs:
        raise MetaOosError("META_OOS_TOO_MANY_EPOCHS")
    if (
        plan.outer_audit_budget_ordinal
        > evaluation_contract.maximum_outer_audit_uses_per_dataset
    ):
        raise MetaOosError("META_OOS_OUTER_AUDIT_BUDGET_EXCEEDED")
    if any(
        epoch.candidate_generation_budget
        > evaluation_contract.maximum_candidate_generation_budget_per_epoch
        or epoch.oos_budget
        > evaluation_contract.maximum_oos_budget_per_epoch
        for epoch in plan.epochs
    ):
        raise MetaOosError("META_OOS_EPOCH_BUDGET_EXCEEDED")


def _validate_decision_context(
    decision: MetaOosPolicyDecisionV1,
    context: MetaOosEpochContextV1,
) -> None:
    if (
        decision.plan_id != context.plan_id
        or decision.plan_hash != context.plan_hash
        or decision.epoch_id != context.epoch_id
        or decision.arm is not context.arm
        or decision.context_hash != context.context_hash
        or decision.created_at > context.decision_at
    ):
        raise MetaOosError("META_OOS_DECISION_CONTEXT_MISMATCH")


def _validate_decision_memory(
    decision: MetaOosPolicyDecisionV1,
    arm: MetaOosPolicyArm,
    memory: MetaOosMemorySnapshotV1 | None,
) -> None:
    if arm is MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER:
        if memory is None or (
            decision.research_memory_snapshot_hash != memory.snapshot_hash
        ):
            raise MetaOosError("ADAPTIVE_MEMORY_BINDING_MISMATCH")
    elif decision.research_memory_snapshot_hash is not None:
        raise MetaOosError("NON_ADAPTIVE_ARM_BINDS_MEMORY")


def _validate_decision_commander(
    plan: ChronologicalMetaOosPlanV1,
    decision: MetaOosPolicyDecisionV1,
) -> None:
    commander_arm = decision.arm in {
        MetaOosPolicyArm.MEMORYLESS_COMMANDER,
        MetaOosPolicyArm.ADAPTIVE_META_CONTROLLER,
    }
    if plan.audit_mode is MetaOosAuditMode.SYNTHETIC_FIXTURE:
        if decision.commander_invocation is not None:
            _validate_commander_invocation(
                decision.commander_invocation,
                plan.commander_binding,
                decision_at=decision.created_at,
            )
        return
    if commander_arm:
        _validate_commander_invocation(
            decision.commander_invocation,
            plan.commander_binding,
            decision_at=decision.created_at,
        )
    elif decision.commander_invocation is not None:
        raise MetaOosError("NON_COMMANDER_ARM_HAS_INVOCATION")


def _validate_commander_invocation(
    invocation: MetaOosCommanderInvocationV1 | None,
    binding: MetaOosCommanderBindingV1,
    *,
    decision_at: datetime,
) -> None:
    if invocation is None:
        raise MetaOosError("META_OOS_COMMANDER_INVOCATION_MISSING")
    if (
        invocation.commander_binding_hash != binding.binding_hash
        or invocation.model_family != binding.model_family
        or invocation.model_version != binding.model_version
        or invocation.reasoning_profile != binding.reasoning_profile
        or invocation.request_schema_hash != binding.request_schema_hash
        or invocation.output_schema_hash != binding.output_schema_hash
        or invocation.invoked_at > require_aware_utc(decision_at)
    ):
        raise MetaOosError("META_OOS_COMMANDER_INVOCATION_MISMATCH")


def _validate_candidate_selection(
    decision: MetaOosPolicyDecisionV1,
    catalog: Mapping[str, MetaOosCandidateAvailabilityV1],
    decision_at: datetime,
) -> None:
    if decision.candidate_id is None:
        return
    candidate = catalog.get(decision.candidate_id)
    if candidate is None:
        raise MetaOosError("META_OOS_CANDIDATE_UNKNOWN")
    if not candidate.is_available(decision_at):
        raise MetaOosError("META_OOS_CANDIDATE_NOT_POINT_IN_TIME")


def _validate_private_epoch(
    private: MetaOosPrivateEpochEvaluation,
    *,
    epoch: MetaOosEpochV1,
    evaluation_contract: MetaOosEvaluationContractV1,
) -> None:
    if private.epoch_id != epoch.epoch_id:
        raise MetaOosError("META_OOS_PRIVATE_EPOCH_MISMATCH")
    if (
        private.candidate_count < 0
        or private.oos_use_count < 0
        or private.promotion_eligible_count < 0
        or len(private.observations) < 2
    ):
        raise MetaOosError("META_OOS_PRIVATE_COUNT_INVALID")
    keys: set[str] = set()
    for item in private.observations:
        occurred = require_aware_utc(item.occurred_at)
        available = require_aware_utc(item.available_at)
        if item.session_key in keys:
            raise MetaOosError("META_OOS_DUPLICATE_PRIVATE_SESSION")
        keys.add(item.session_key)
        if not (
            epoch.forward_window_start
            <= occurred
            <= epoch.forward_window_end
            and occurred <= available <= epoch.outcome_available_at
        ):
            raise MetaOosError("META_OOS_PRIVATE_PIT_VIOLATION")
        if (
            not math.isfinite(item.net_return)
            or not math.isfinite(item.turnover)
            or item.turnover < 0
            or item.net_return <= -1
            or abs(item.net_return)
            > evaluation_contract.maximum_absolute_daily_return
        ):
            raise MetaOosError("META_OOS_PRIVATE_METRIC_INVALID")
    if not math.isfinite(private.learning_reward):
        raise MetaOosError("META_OOS_PRIVATE_REWARD_INVALID")


def _build_learning_outcome(
    *,
    arm: MetaOosPolicyArm,
    epoch: MetaOosEpochV1,
    decision: MetaOosPolicyDecisionV1,
    private: MetaOosPrivateEpochEvaluation,
) -> MetaOosLearningOutcomeV1:
    outcome_id = stable_id(
        "meta-oos-learning-outcome",
        decision.decision_hash,
        epoch.outcome_available_at,
    )
    payload = {
        "schema_version": "meta_oos_learning_outcome_v1",
        "outcome_id": outcome_id,
        "arm": arm,
        "epoch_id": epoch.epoch_id,
        "context_key": epoch.context_key,
        "action_kind": decision.action_kind or "NO_CHANGE",
        "information_role": private.information_role,
        "available_at": epoch.outcome_available_at,
        "matured": True,
        "reward": float(private.learning_reward),
        "technical_failure": private.technical_failure,
    }
    return MetaOosLearningOutcomeV1.model_validate(
        {**payload, "outcome_hash": canonical_hash(payload)}
    )


def _private_outcome_hash(
    private: MetaOosPrivateEpochEvaluation,
) -> str:
    return canonical_hash(
        {
            "epoch_id": private.epoch_id,
            "observations": tuple(
                {
                    "session_key": item.session_key,
                    "occurred_at": require_aware_utc(item.occurred_at),
                    "available_at": require_aware_utc(item.available_at),
                    "net_return": item.net_return,
                    "turnover": item.turnover,
                    "regime": item.regime,
                }
                for item in private.observations
            ),
            "candidate_count": private.candidate_count,
            "oos_use_count": private.oos_use_count,
            "promotion_eligible_count": private.promotion_eligible_count,
            "technical_failure": private.technical_failure,
            "information_role": private.information_role,
            "learning_reward": private.learning_reward,
            "candidate_artifact_hash": private.candidate_artifact_hash,
        }
    )


def _build_epoch_audit_record(
    *,
    plan: ChronologicalMetaOosPlanV1,
    epoch: MetaOosEpochV1,
    arm: MetaOosPolicyArm,
    decision: MetaOosPolicyDecisionV1,
    memory: MetaOosMemorySnapshotV1 | None,
    private: MetaOosPrivateEpochEvaluation,
) -> MetaOosEpochArmAuditRecordV1:
    private_hash = _private_outcome_hash(private)
    record_id = stable_id(
        "meta-oos-epoch-arm-record",
        plan.plan_hash,
        epoch.epoch_id,
        arm,
    )
    payload = {
        "schema_version": "meta_oos_epoch_arm_audit_record_v1",
        "record_id": record_id,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "epoch_id": epoch.epoch_id,
        "arm": arm,
        "decision_hash": decision.decision_hash,
        "memory_snapshot_hash": (
            None if memory is None else memory.snapshot_hash
        ),
        "candidate_artifact_hash": private.candidate_artifact_hash,
        "private_outcome_hash": private_hash,
        "session_count": len(private.observations),
        "experiment_count": (
            1 if decision.decision_kind is MetaOosDecisionKind.RUN_ACTION else 0
        ),
        "candidate_count": private.candidate_count,
        "oos_use_count": private.oos_use_count,
        "positive_matured_outcome_count": (
            1
            if private.information_role
            is ExperimentInformationRole.LEARNING_FORWARD
            and private.learning_reward > 0
            and not private.technical_failure
            else 0
        ),
        "technical_failure_count": 1 if private.technical_failure else 0,
        "promotion_eligible_count": private.promotion_eligible_count,
        "outcome_available_at": epoch.outcome_available_at,
        "created_at": epoch.outcome_available_at,
        "real_order_routing": False,
    }
    return MetaOosEpochArmAuditRecordV1.model_validate(
        {**payload, "record_hash": canonical_hash(payload)}
    )


def _build_arm_aggregate(
    *,
    arm: MetaOosPolicyArm,
    sessions: Sequence[
        tuple[MetaOosPrivateSessionObservation, str, float | None]
    ],
    audit_records: Sequence[MetaOosEpochArmAuditRecordV1],
    prediction_pairs: Sequence[tuple[float, float]],
    evaluation_contract: MetaOosEvaluationContractV1,
) -> MetaOosArmAggregateV1:
    returns = tuple(item[0].net_return for item in sessions)
    turnovers = tuple(item[0].turnover for item in sessions)
    risk_free = (0.0,) * len(returns)
    sharpe = calculate_sample_sharpe(
        returns=returns,
        risk_free_returns=risk_free,
        annualization_sessions=evaluation_contract.annualization_sessions,
        variance_epsilon=1e-12,
    )
    mean = sum(returns) / len(returns)
    sample_variance = sum((item - mean) ** 2 for item in returns) / (
        len(returns) - 1
    )
    annualized_volatility = math.sqrt(sample_variance) * math.sqrt(
        evaluation_contract.annualization_sessions
    )
    nav = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        nav *= 1.0 + value
        peak = max(peak, nav)
        max_drawdown = max(max_drawdown, (peak - nav) / peak)
    sorted_returns = tuple(sorted(returns))
    tail_index = min(
        len(sorted_returns) - 1,
        max(
            0,
            math.floor(
                evaluation_contract.tail_quantile
                * (len(sorted_returns) - 1)
            ),
        ),
    )
    tail_loss = max(0.0, -sorted_returns[tail_index])
    regime_values: dict[str, list[float]] = defaultdict(list)
    action_values: dict[str, list[float]] = defaultdict(list)
    for observation, action, _ in sessions:
        regime_values[observation.regime].append(observation.net_return)
        action_values[action].append(observation.net_return)
    errors = tuple(
        actual - predicted for predicted, actual in prediction_pairs
    )
    payload = {
        "schema_version": "meta_oos_arm_aggregate_v1",
        "arm": arm,
        "net_sequence_return": nav - 1.0,
        "annualized_volatility": annualized_volatility,
        "portfolio_sharpe": sharpe,
        "maximum_drawdown": max_drawdown,
        "tail_loss": tail_loss,
        "annualized_turnover": (
            sum(turnovers)
            / len(turnovers)
            * evaluation_contract.annualization_sessions
        ),
        "experiment_count": sum(item.experiment_count for item in audit_records),
        "candidate_count": sum(item.candidate_count for item in audit_records),
        "oos_use_count": sum(item.oos_use_count for item in audit_records),
        "positive_matured_outcome_count": sum(
            item.positive_matured_outcome_count for item in audit_records
        ),
        "technical_failure_count": sum(
            item.technical_failure_count for item in audit_records
        ),
        "promotion_eligible_count": sum(
            item.promotion_eligible_count for item in audit_records
        ),
        "regime_results": {
            key: _group_aggregate(values)
            for key, values in sorted(regime_values.items())
        },
        "action_results": {
            key: _group_aggregate(values)
            for key, values in sorted(action_values.items())
        },
        "prediction_calibration": MetaOosPredictionCalibrationV1(
            prediction_count=len(errors),
            mean_error=(
                None if not errors else sum(errors) / len(errors)
            ),
            mean_absolute_error=(
                None
                if not errors
                else sum(abs(item) for item in errors) / len(errors)
            ),
        ),
    }
    return MetaOosArmAggregateV1.model_validate(
        {**payload, "arm_result_hash": canonical_hash(payload)}
    )


def _adaptive_verdict_reason_codes(
    *,
    adaptive: MetaOosArmAggregateV1,
    adaptive_comparisons: Sequence[MetaOosPairedComparisonV1],
    adaptive_research_efficiency: float,
    evaluation_contract: MetaOosEvaluationContractV1,
) -> tuple[str, ...]:
    required_baselines = (
        MetaOosPolicyArm.STATIC_CHAMPION,
        MetaOosPolicyArm.FIXED_RECALIBRATION,
        MetaOosPolicyArm.MEMORYLESS_COMMANDER,
    )
    by_baseline = {
        item.baseline_arm: item for item in adaptive_comparisons
    }
    reasons: list[str] = []
    if set(by_baseline) != set(required_baselines) or len(
        adaptive_comparisons
    ) != len(required_baselines):
        reasons.append("ADAPTIVE_COMPARISON_MATRIX_INCOMPLETE")
    for baseline in required_baselines:
        comparison = by_baseline.get(baseline)
        if comparison is None:
            continue
        if not (
            comparison.delta_sharpe_lcb
            > evaluation_contract.minimum_adaptive_delta_sharpe_lcb
        ):
            reasons.append(
                f"ADAPTIVE_LCB_NOT_ABOVE_{baseline.value}"
            )
    if (
        adaptive_research_efficiency
        < evaluation_contract.minimum_research_efficiency
    ):
        reasons.append("ADAPTIVE_RESEARCH_EFFICIENCY_TOO_LOW")
    if (
        adaptive.maximum_drawdown
        > evaluation_contract.maximum_allowed_drawdown
    ):
        reasons.append("ADAPTIVE_MAXIMUM_DRAWDOWN_EXCEEDED")
    return tuple(reasons)


def _group_aggregate(values: Sequence[float]) -> MetaOosGroupAggregateV1:
    return MetaOosGroupAggregateV1(
        observations=len(values),
        mean_net_return=sum(values) / len(values),
        positive_fraction=sum(item > 0 for item in values) / len(values),
    )


def _build_pairwise_comparisons(
    *,
    plan: ChronologicalMetaOosPlanV1,
    private_sessions: Mapping[
        MetaOosPolicyArm,
        Sequence[
            tuple[MetaOosPrivateSessionObservation, str, float | None]
        ],
    ],
    evaluation_contract: MetaOosEvaluationContractV1,
) -> tuple[MetaOosPairedComparisonV1, ...]:
    values: list[MetaOosPairedComparisonV1] = []
    baselines = (
        MetaOosPolicyArm.STATIC_CHAMPION,
        MetaOosPolicyArm.FIXED_RECALIBRATION,
        MetaOosPolicyArm.MEMORYLESS_COMMANDER,
    )
    for candidate_arm in META_OOS_POLICY_ARMS:
        candidate_returns = tuple(
            item[0].net_return for item in private_sessions[candidate_arm]
        )
        for baseline_arm in baselines:
            if candidate_arm is baseline_arm:
                continue
            baseline_returns = tuple(
                item[0].net_return for item in private_sessions[baseline_arm]
            )
            seed = int(
                canonical_hash(
                    {
                        "plan_hash": plan.plan_hash,
                        "configured_seed": (
                            plan.bootstrap_contract.configured_seed
                        ),
                        "candidate_arm": candidate_arm,
                        "baseline_arm": baseline_arm,
                    }
                )[:16],
                16,
            )
            try:
                metric = evaluate_paired_sharpe_returns(
                    candidate_returns=candidate_returns,
                    baseline_returns=baseline_returns,
                    risk_free_returns=(0.0,) * len(candidate_returns),
                    annualization_sessions=(
                        evaluation_contract.annualization_sessions
                    ),
                    bootstrap=plan.bootstrap_contract,
                    deterministic_seed=seed,
                )
            except PortfolioDeltaSharpeError as exc:
                raise MetaOosError(
                    f"META_OOS_PAIRED_SHARPE_{exc.reason_code}"
                ) from exc
            payload = {
                "schema_version": "meta_oos_paired_comparison_v1",
                "candidate_arm": candidate_arm,
                "baseline_arm": baseline_arm,
                "common_sessions": len(candidate_returns),
                "delta_sharpe_point": metric.delta_sharpe_point,
                "delta_sharpe_lcb": metric.delta_sharpe_lcb,
                "delta_sharpe_ucb": metric.delta_sharpe_ucb,
            }
            values.append(
                MetaOosPairedComparisonV1.model_validate(
                    {
                        **payload,
                        "comparison_hash": canonical_hash(payload),
                    }
                )
            )
    return tuple(values)


__all__ = [
    "META_OOS_POLICY_ARMS",
    "ChronologicalMetaOosPlanV1",
    "ChronologicalMetaOosResultV1",
    "ChronologicalMetaOosRunV1",
    "DeterministicSyntheticMetaOosEnvironment",
    "FixedRecalibrationPolicyAdapter",
    "MetaOosAuditMode",
    "MetaOosBudgetV1",
    "MetaOosCandidateAvailabilityV1",
    "MetaOosCommanderBindingV1",
    "MetaOosCommanderInvocationV1",
    "MetaOosDecisionKind",
    "MetaOosEnvironment",
    "MetaOosEpochArmAuditRecordV1",
    "MetaOosEpochContextV1",
    "MetaOosEpochV1",
    "MetaOosError",
    "MetaOosEvaluationContractV1",
    "MetaOosLearningOutcomeV1",
    "MetaOosMemorySnapshotV1",
    "MetaOosOuterAuditReservationV1",
    "MetaOosPolicyArm",
    "MetaOosPolicyDecisionV1",
    "RecordedAdaptiveCommanderAdapter",
    "RecordedMemorylessCommanderAdapter",
    "ResearchPolicyAdapter",
    "StaticChampionPolicyAdapter",
    "SyntheticPolicyAdapter",
    "build_chronological_meta_oos_plan",
    "build_meta_oos_commander_binding",
    "build_meta_oos_commander_invocation",
    "build_meta_oos_evaluation_contract",
    "build_meta_oos_memory_snapshot",
    "build_meta_oos_outer_audit_reservation",
    "build_meta_oos_policy_decision",
    "run_chronological_meta_oos",
    "verify_chronological_meta_oos_plan",
    "verify_chronological_meta_oos_result",
]
