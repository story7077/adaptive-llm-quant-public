from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from statistics import fmean
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.research.contracts import HASH_PATTERN, IDENTIFIER_PATTERN
from trading.research.experiment_outcomes import (
    ExperimentInformationRole,
    ExperimentOutcomeEventV1,
    ResearchActionKind,
    ResearchMemorySnapshotV1,
)


class MetaControllerReasonCode(StrEnum):
    EXACT_CONTEXT = "EXACT_CONTEXT"
    BACKOFF_REGIME_FAILURE = "BACKOFF_REGIME_FAILURE"
    BACKOFF_FAILURE = "BACKOFF_FAILURE"
    BACKOFF_REGIME = "BACKOFF_REGIME"
    BACKOFF_ACTION = "BACKOFF_ACTION"
    UNTRIED_EXPLORATION = "UNTRIED_EXPLORATION"
    POSITIVE_MATURED_REWARD = "POSITIVE_MATURED_REWARD"
    TECHNICAL_FAILURE_PENALTY = "TECHNICAL_FAILURE_PENALTY"


class ResearchContextV1(DomainModel):
    schema_version: Literal["research_context_v1"] = "research_context_v1"
    regime_cluster_id: str = Field(pattern=IDENTIFIER_PATTERN)
    failure_cluster_id: str = Field(pattern=IDENTIFIER_PATTERN)
    portfolio_exposure_cluster_id: str = Field(pattern=IDENTIFIER_PATTERN)
    context_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"context_hash"})
        if canonical_hash(payload) != self.context_hash:
            raise ValueError("research context hash mismatch")
        return self


class MetaControllerObservationV1(DomainModel):
    schema_version: Literal[
        "meta_controller_observation_v1"
    ] = "meta_controller_observation_v1"
    event_hash: str = Field(pattern=HASH_PATTERN)
    research_cycle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    primary_action_kind: ResearchActionKind
    context: ResearchContextV1 | None
    available_at: datetime
    technical_success: bool | None
    portfolio_delta_sharpe_lcb: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    turnover_delta: float | None = Field(default=None, allow_inf_nan=False)
    drawdown_delta: float | None = Field(default=None, allow_inf_nan=False)
    cost_delta_bps: float | None = Field(default=None, allow_inf_nan=False)
    complexity_delta: float = Field(allow_inf_nan=False)
    observation_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("available_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.primary_action_kind is ResearchActionKind.UNKNOWN_LEGACY:
            raise ValueError("legacy action cannot become a Meta Controller observation")
        payload = self.model_dump(mode="python", exclude={"observation_hash"})
        if canonical_hash(payload) != self.observation_hash:
            raise ValueError("Meta Controller observation hash mismatch")
        return self


class MetaControllerTrainingViewV1(DomainModel):
    schema_version: Literal[
        "meta_controller_training_view_v1"
    ] = "meta_controller_training_view_v1"
    research_memory_snapshot_hash: str = Field(pattern=HASH_PATTERN)
    data_available_cutoff: datetime
    observations: tuple[MetaControllerObservationV1, ...]
    view_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("data_available_cutoff", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_view(self) -> Self:
        if any(
            observation.available_at > self.data_available_cutoff
            for observation in self.observations
        ):
            raise ValueError("Meta Controller training view contains future data")
        ordering = tuple(
            sorted(
                self.observations,
                key=lambda item: (
                    item.available_at,
                    item.event_hash,
                ),
            )
        )
        if ordering != self.observations:
            raise ValueError("Meta Controller observations must be ordered")
        if len({item.event_hash for item in self.observations}) != len(
            self.observations
        ):
            raise ValueError("Meta Controller observations must be unique")
        payload = self.model_dump(mode="python", exclude={"view_hash"})
        if canonical_hash(payload) != self.view_hash:
            raise ValueError("Meta Controller training view hash mismatch")
        return self


class MetaControllerParametersV1(DomainModel):
    schema_version: Literal[
        "meta_controller_parameters_v1"
    ] = "meta_controller_parameters_v1"
    policy_version: str = Field(pattern=IDENTIFIER_PATTERN)
    maximum_actions_per_cycle: int = Field(gt=0)
    prior_strength: float = Field(gt=0, allow_inf_nan=False)
    exploration_coefficient: float = Field(ge=0, allow_inf_nan=False)
    exploration_floor: float = Field(ge=0, allow_inf_nan=False)
    technical_failure_weight: float = Field(ge=0, allow_inf_nan=False)
    reward_clip: float = Field(gt=0, allow_inf_nan=False)
    turnover_penalty_weight: float = Field(ge=0, allow_inf_nan=False)
    turnover_scale: float = Field(gt=0, allow_inf_nan=False)
    drawdown_penalty_weight: float = Field(ge=0, allow_inf_nan=False)
    drawdown_scale: float = Field(gt=0, allow_inf_nan=False)
    cost_penalty_weight: float = Field(ge=0, allow_inf_nan=False)
    cost_scale_bps: float = Field(gt=0, allow_inf_nan=False)
    complexity_penalty_weight: float = Field(ge=0, allow_inf_nan=False)
    complexity_scale: float = Field(gt=0, allow_inf_nan=False)


class RankedResearchActionV1(DomainModel):
    action_kind: ResearchActionKind
    score: float = Field(allow_inf_nan=False)
    shrunk_reward_mean: float = Field(allow_inf_nan=False)
    exploration_bonus: float = Field(ge=0, allow_inf_nan=False)
    technical_failure_penalty: float = Field(ge=0, allow_inf_nan=False)
    matured_sample_count: int = Field(ge=0)
    total_attempt_count: int = Field(ge=0)
    allocated_submission_budget: int = Field(ge=0)
    reason_codes: tuple[MetaControllerReasonCode, ...]

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.action_kind is ResearchActionKind.UNKNOWN_LEGACY:
            raise ValueError("UNKNOWN_LEGACY cannot be ranked")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("Meta Controller reason codes must be unique")
        return self


class ResearchActionPlanV1(DomainModel):
    schema_version: Literal[
        "research_action_plan_v1"
    ] = "research_action_plan_v1"
    action_plan_id: str = Field(pattern=IDENTIFIER_PATTERN)
    research_cycle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    policy_version: str = Field(pattern=IDENTIFIER_PATTERN)
    research_memory_snapshot_hash: str = Field(pattern=HASH_PATTERN)
    training_view_hash: str = Field(pattern=HASH_PATTERN)
    context: ResearchContextV1
    context_hash: str = Field(pattern=HASH_PATTERN)
    config_hash: str = Field(pattern=HASH_PATTERN)
    ranked_actions: tuple[RankedResearchActionV1, ...] = Field(min_length=1)
    maximum_actions: int = Field(gt=0)
    maximum_total_submissions: int = Field(gt=0)
    idempotency_key: str = Field(pattern=IDENTIFIER_PATTERN)
    generated_at: datetime
    plan_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("generated_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if self.context_hash != self.context.context_hash:
            raise ValueError("action plan context hash mismatch")
        actions = tuple(item.action_kind for item in self.ranked_actions)
        if len(set(actions)) != len(actions):
            raise ValueError("action plan contains duplicate actions")
        if sum(
            item.allocated_submission_budget
            for item in self.ranked_actions
        ) != self.maximum_total_submissions:
            raise ValueError("action plan submission budget arithmetic mismatch")
        funded = sum(
            item.allocated_submission_budget > 0
            for item in self.ranked_actions
        )
        if funded > self.maximum_actions:
            raise ValueError("action plan funds too many actions")
        ordering = tuple(
            sorted(
                self.ranked_actions,
                key=lambda item: (-item.score, item.action_kind.value),
            )
        )
        if ordering != self.ranked_actions:
            raise ValueError("action plan ranking is not deterministic")
        payload = self.model_dump(mode="python", exclude={"plan_hash"})
        if canonical_hash(payload) != self.plan_hash:
            raise ValueError("research action plan hash mismatch")
        return self

    def permitted_action_kinds(self) -> frozenset[ResearchActionKind]:
        return frozenset(
            item.action_kind
            for item in self.ranked_actions
            if item.allocated_submission_budget > 0
        )


def build_research_context(
    *,
    regime_cluster_id: str,
    failure_cluster_id: str,
    portfolio_exposure_cluster_id: str,
) -> ResearchContextV1:
    payload = {
        "schema_version": "research_context_v1",
        "regime_cluster_id": regime_cluster_id,
        "failure_cluster_id": failure_cluster_id,
        "portfolio_exposure_cluster_id": portfolio_exposure_cluster_id,
    }
    return ResearchContextV1.model_validate(
        {**payload, "context_hash": canonical_hash(payload)}
    )


def build_meta_controller_training_view(
    *,
    snapshot: ResearchMemorySnapshotV1,
    verified_events: tuple[ExperimentOutcomeEventV1, ...],
    contexts_by_research_cycle: Mapping[str, ResearchContextV1],
) -> MetaControllerTrainingViewV1:
    """Create the controller-only view from an exact verified snapshot prefix."""

    event_by_hash = {event.event_hash: event for event in verified_events}
    if len(event_by_hash) != len(verified_events):
        raise ValueError("verified event set contains duplicate hashes")
    if set(event_by_hash) != set(snapshot.included_event_hashes):
        raise ValueError("verified events do not match the immutable memory snapshot")
    observations: list[MetaControllerObservationV1] = []
    for event_hash in snapshot.included_event_hashes:
        event = event_by_hash[event_hash]
        if event.available_at > snapshot.data_available_cutoff:
            raise ValueError("snapshot event became available after its cutoff")
        if (
            event.information_role
            is not ExperimentInformationRole.LEARNING_FORWARD
            or event.primary_action_kind is ResearchActionKind.UNKNOWN_LEGACY
        ):
            continue
        has_economic_reward = (
            event.eligible_for_meta_training
            and event.portfolio_delta_sharpe_lcb is not None
        )
        has_technical_observation = event.technical_success is not None
        if not has_economic_reward and not has_technical_observation:
            continue
        payload = {
            "schema_version": "meta_controller_observation_v1",
            "event_hash": event.event_hash,
            "research_cycle_id": event.research_cycle_id,
            "primary_action_kind": event.primary_action_kind,
            "context": contexts_by_research_cycle.get(
                event.research_cycle_id
            ),
            "available_at": event.available_at,
            "technical_success": event.technical_success,
            "portfolio_delta_sharpe_lcb": (
                event.portfolio_delta_sharpe_lcb
                if has_economic_reward
                else None
            ),
            "turnover_delta": (
                event.turnover_delta if has_economic_reward else None
            ),
            "drawdown_delta": (
                event.drawdown_delta if has_economic_reward else None
            ),
            "cost_delta_bps": (
                event.cost_delta_bps if has_economic_reward else None
            ),
            "complexity_delta": event.complexity_delta,
        }
        observations.append(
            MetaControllerObservationV1.model_validate(
                {
                    **payload,
                    "observation_hash": canonical_hash(payload),
                }
            )
        )
    observations.sort(key=lambda item: (item.available_at, item.event_hash))
    view_payload = {
        "schema_version": "meta_controller_training_view_v1",
        "research_memory_snapshot_hash": snapshot.snapshot_hash,
        "data_available_cutoff": snapshot.data_available_cutoff,
        "observations": tuple(observations),
    }
    return MetaControllerTrainingViewV1.model_validate(
        {**view_payload, "view_hash": canonical_hash(view_payload)}
    )


def build_research_action_plan(
    *,
    research_cycle_id: str,
    snapshot: ResearchMemorySnapshotV1,
    training_view: MetaControllerTrainingViewV1,
    context: ResearchContextV1,
    parameters: MetaControllerParametersV1,
    config_hash: str,
    available_action_kinds: tuple[ResearchActionKind, ...],
    maximum_total_submissions: int,
    idempotency_key: str,
    generated_at: datetime,
) -> ResearchActionPlanV1:
    if training_view.research_memory_snapshot_hash != snapshot.snapshot_hash:
        raise ValueError("Meta Controller view belongs to another snapshot")
    if any(
        observation.event_hash not in snapshot.included_event_hashes
        for observation in training_view.observations
    ):
        raise ValueError("Meta Controller view is outside its memory snapshot")
    generated = require_aware_utc(generated_at)
    if generated < snapshot.created_at:
        raise ValueError("action plan cannot predate its memory snapshot")
    if maximum_total_submissions <= 0:
        raise ValueError("Meta Controller requires a positive submission budget")
    actions = tuple(sorted(set(available_action_kinds), key=lambda item: item.value))
    if not actions or ResearchActionKind.UNKNOWN_LEGACY in actions:
        raise ValueError("available actions must be typed non-legacy actions")

    rewards = tuple(
        _economic_reward(observation, parameters)
        for observation in training_view.observations
        if observation.portfolio_delta_sharpe_lcb is not None
    )
    global_mean = 0.0 if not rewards else fmean(rewards)
    total_matured = len(rewards)
    entries = tuple(
        _score_action(
            action=action,
            context=context,
            observations=training_view.observations,
            parameters=parameters,
            global_mean=global_mean,
            total_matured=total_matured,
        )
        for action in actions
    )
    ranked = tuple(
        sorted(entries, key=lambda item: (-item.score, item.action_kind.value))
    )
    funded_count = min(
        parameters.maximum_actions_per_cycle,
        maximum_total_submissions,
        len(ranked),
    )
    base_budget, remainder = divmod(maximum_total_submissions, funded_count)
    budget_by_action = {
        item.action_kind: (
            base_budget + (1 if index < remainder else 0)
            if index < funded_count
            else 0
        )
        for index, item in enumerate(ranked)
    }
    ranked_with_budget = tuple(
        item.model_copy(
            update={
                "allocated_submission_budget": budget_by_action[
                    item.action_kind
                ]
            }
        )
        for item in ranked
    )
    payload = {
        "schema_version": "research_action_plan_v1",
        "action_plan_id": stable_id(
            "research-action-plan",
            research_cycle_id,
            snapshot.snapshot_hash,
            training_view.view_hash,
            context.context_hash,
            config_hash,
            generated,
        ),
        "research_cycle_id": research_cycle_id,
        "policy_version": parameters.policy_version,
        "research_memory_snapshot_hash": snapshot.snapshot_hash,
        "training_view_hash": training_view.view_hash,
        "context": context,
        "context_hash": context.context_hash,
        "config_hash": config_hash,
        "ranked_actions": ranked_with_budget,
        "maximum_actions": parameters.maximum_actions_per_cycle,
        "maximum_total_submissions": maximum_total_submissions,
        "idempotency_key": idempotency_key,
        "generated_at": generated,
    }
    return ResearchActionPlanV1.model_validate(
        {**payload, "plan_hash": canonical_hash(payload)}
    )


def _economic_reward(
    observation: MetaControllerObservationV1,
    parameters: MetaControllerParametersV1,
) -> float:
    if observation.portfolio_delta_sharpe_lcb is None:
        raise ValueError("technical-only observation has no economic reward")
    raw = (
        observation.portfolio_delta_sharpe_lcb
        - parameters.turnover_penalty_weight
        * max(0.0, (observation.turnover_delta or 0.0) / parameters.turnover_scale)
        - parameters.drawdown_penalty_weight
        * max(0.0, (observation.drawdown_delta or 0.0) / parameters.drawdown_scale)
        - parameters.cost_penalty_weight
        * max(0.0, (observation.cost_delta_bps or 0.0) / parameters.cost_scale_bps)
        - parameters.complexity_penalty_weight
        * max(0.0, observation.complexity_delta / parameters.complexity_scale)
    )
    return max(-parameters.reward_clip, min(parameters.reward_clip, raw))


def _score_action(
    *,
    action: ResearchActionKind,
    context: ResearchContextV1,
    observations: tuple[MetaControllerObservationV1, ...],
    parameters: MetaControllerParametersV1,
    global_mean: float,
    total_matured: int,
) -> RankedResearchActionV1:
    action_observations = tuple(
        item for item in observations if item.primary_action_kind is action
    )
    buckets = (
        (
            MetaControllerReasonCode.EXACT_CONTEXT,
            tuple(
                item
                for item in action_observations
                if item.context == context
            ),
        ),
        (
            MetaControllerReasonCode.BACKOFF_REGIME_FAILURE,
            tuple(
                item
                for item in action_observations
                if item.context is not None
                and item.context.regime_cluster_id
                == context.regime_cluster_id
                and item.context.failure_cluster_id
                == context.failure_cluster_id
            ),
        ),
        (
            MetaControllerReasonCode.BACKOFF_FAILURE,
            tuple(
                item
                for item in action_observations
                if item.context is not None
                and item.context.failure_cluster_id
                == context.failure_cluster_id
            ),
        ),
        (
            MetaControllerReasonCode.BACKOFF_REGIME,
            tuple(
                item
                for item in action_observations
                if item.context is not None
                and item.context.regime_cluster_id
                == context.regime_cluster_id
            ),
        ),
        (
            MetaControllerReasonCode.BACKOFF_ACTION,
            action_observations,
        ),
    )
    selected_index = next(
        (
            index
            for index, (_, bucket) in enumerate(buckets[:-1])
            if bucket
        ),
        len(buckets) - 1,
    )
    selected_reason, selected = buckets[selected_index]
    parent_mean = global_mean
    for _, parent_bucket in buckets[selected_index + 1 :]:
        parent_rewards = tuple(
            _economic_reward(item, parameters)
            for item in parent_bucket
            if item.portfolio_delta_sharpe_lcb is not None
        )
        if parent_rewards:
            parent_mean = fmean(parent_rewards)
            break
    selected_rewards = tuple(
        _economic_reward(item, parameters)
        for item in selected
        if item.portfolio_delta_sharpe_lcb is not None
    )
    matured_count = len(selected_rewards)
    shrunk_mean = (
        parameters.prior_strength * parent_mean + sum(selected_rewards)
    ) / (parameters.prior_strength + matured_count)
    formula_bonus = parameters.exploration_coefficient * math.sqrt(
        math.log1p(total_matured)
        / (parameters.prior_strength + matured_count)
    )
    exploration_bonus = max(parameters.exploration_floor, formula_bonus)
    attempts = len(selected)
    technical_failures = sum(
        item.technical_success is False for item in selected
    )
    technical_penalty = parameters.technical_failure_weight * (
        technical_failures / max(1, attempts)
    )
    score = shrunk_mean + exploration_bonus - technical_penalty
    reasons = [selected_reason]
    if not action_observations:
        reasons.append(MetaControllerReasonCode.UNTRIED_EXPLORATION)
    if any(value > 0 for value in selected_rewards):
        reasons.append(MetaControllerReasonCode.POSITIVE_MATURED_REWARD)
    if technical_failures:
        reasons.append(
            MetaControllerReasonCode.TECHNICAL_FAILURE_PENALTY
        )
    return RankedResearchActionV1(
        action_kind=action,
        score=score,
        shrunk_reward_mean=shrunk_mean,
        exploration_bonus=exploration_bonus,
        technical_failure_penalty=technical_penalty,
        matured_sample_count=matured_count,
        total_attempt_count=attempts,
        allocated_submission_budget=0,
        reason_codes=tuple(reasons),
    )
