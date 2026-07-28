from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from enum import StrEnum
from statistics import fmean
from typing import Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.research.contracts import (
    HASH_PATTERN,
    IDENTIFIER_PATTERN,
    SYMBOL_PATTERN,
    VERSION_PATTERN,
    AlgorithmProposalV1,
)
from trading.research.sandbox_contract import (
    CANDIDATE_PATCH_POLICY_V2,
    V2_ALLOWED_PATTERNS,
)


class ResearchActionKind(StrEnum):
    ADD_FEATURE = "ADD_FEATURE"
    REMOVE_FEATURE = "REMOVE_FEATURE"
    CHANGE_SIGNAL_FORM = "CHANGE_SIGNAL_FORM"
    ADD_REGIME_GATE = "ADD_REGIME_GATE"
    CHANGE_POSITION_SIZING = "CHANGE_POSITION_SIZING"
    CHANGE_EXIT_RULE = "CHANGE_EXIT_RULE"
    ADD_DIVERSIFYING_SLEEVE = "ADD_DIVERSIFYING_SLEEVE"
    RECALIBRATE_PARAMETER = "RECALIBRATE_PARAMETER"
    RETIRE_REDUNDANT_SLEEVE = "RETIRE_REDUNDANT_SLEEVE"
    REQUEST_NEW_DATA = "REQUEST_NEW_DATA"
    UNKNOWN_LEGACY = "UNKNOWN_LEGACY"


class ExperimentInformationRole(StrEnum):
    DISCOVERY = "DISCOVERY"
    LEARNING_FORWARD = "LEARNING_FORWARD"
    PROMOTION_OOS = "PROMOTION_OOS"
    META_AUDIT = "META_AUDIT"


class ExperimentMaturityStatus(StrEnum):
    PENDING = "PENDING"
    MATURED = "MATURED"
    CENSORED = "CENSORED"
    INVALIDATED = "INVALIDATED"
    SUPERSEDED = "SUPERSEDED"


class ExperimentOutcomeEventKind(StrEnum):
    EXPERIMENT_REGISTERED = "EXPERIMENT_REGISTERED"
    TECHNICAL_OUTCOME_RECORDED = "TECHNICAL_OUTCOME_RECORDED"
    ECONOMIC_OUTCOME_MATURED = "ECONOMIC_OUTCOME_MATURED"
    OUTCOME_CENSORED = "OUTCOME_CENSORED"
    OUTCOME_INVALIDATED = "OUTCOME_INVALIDATED"
    OUTCOME_CORRECTED = "OUTCOME_CORRECTED"


class ExperimentStage(StrEnum):
    PROPOSAL = "PROPOSAL"
    BUILD = "BUILD"
    TEST = "TEST"
    REPLAY = "REPLAY"
    FALSIFICATION = "FALSIFICATION"
    OOS = "OOS"
    SHADOW = "SHADOW"
    PROMOTION = "PROMOTION"
    FORWARD = "FORWARD"
    DUPLICATE = "DUPLICATE"


class PredictedPortfolioDeltaSharpeV1(DomainModel):
    lower: float = Field(allow_inf_nan=False)
    median: float = Field(allow_inf_nan=False)
    upper: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if not self.lower <= self.median <= self.upper:
            raise ValueError("predicted portfolio delta Sharpe interval is not ordered")
        return self


class AlgorithmProposalV2(DomainModel):
    """Recursive-research proposal without changing the stored V1 contract."""

    schema_version: Literal["algorithm_proposal_v2"] = "algorithm_proposal_v2"
    proposal_id: str = Field(pattern=IDENTIFIER_PATTERN)
    hypothesis_id: str = Field(pattern=IDENTIFIER_PATTERN)
    hypothesis: str = Field(min_length=1, max_length=4000)
    economic_mechanism: str = Field(min_length=1, max_length=4000)
    why_current_model_failed: str = Field(min_length=1, max_length=4000)
    parent_strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parent_strategy_version: str = Field(pattern=VERSION_PATTERN)
    proposed_strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    proposed_strategy_version: str = Field(pattern=VERSION_PATTERN)
    target_horizon: str = Field(min_length=1, max_length=120)
    target_universe: list[str] = Field(min_length=1, max_length=2000)
    required_data: list[str] = Field(min_length=1, max_length=200)
    feature_changes: list[str] = Field(default_factory=list, max_length=200)
    signal_formula_changes: list[str] = Field(default_factory=list, max_length=200)
    entry_rule_changes: list[str] = Field(default_factory=list, max_length=100)
    exit_rule_changes: list[str] = Field(default_factory=list, max_length=100)
    position_sizing_changes: list[str] = Field(
        default_factory=list,
        max_length=100,
    )
    regime_activation_changes: list[str] = Field(
        default_factory=list,
        max_length=100,
    )
    calibration_changes: list[str] = Field(default_factory=list, max_length=100)
    expected_edge_source: str = Field(min_length=1, max_length=3000)
    expected_failure_modes: list[str] = Field(min_length=1, max_length=100)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=100)
    placebo_tests: list[str] = Field(min_length=1, max_length=100)
    stress_tests: list[str] = Field(min_length=1, max_length=100)
    minimum_economic_effect: dict[str, JsonValue]
    estimated_capacity: dict[str, JsonValue]
    estimated_turnover: dict[str, JsonValue]
    estimated_cost_sensitivity: dict[str, JsonValue]
    files_allowed_to_change: list[str] = Field(min_length=1, max_length=100)
    tests_required: list[str] = Field(min_length=1, max_length=200)
    evidence_source_ids: list[str] = Field(min_length=1, max_length=500)
    raw_confidence: float = Field(ge=0, le=1)
    patch_policy_version: Literal[
        "candidate_patch_policy_v2"
    ] = CANDIDATE_PATCH_POLICY_V2
    primary_action_kind: ResearchActionKind
    secondary_action_kinds: tuple[ResearchActionKind, ...] = Field(
        default=(),
        max_length=3,
    )
    mechanism_tags: tuple[str, ...] = Field(min_length=1, max_length=64)
    predicted_portfolio_delta_sharpe: PredictedPortfolioDeltaSharpeV1
    predicted_failure_codes: tuple[str, ...] = Field(default=(), max_length=64)
    complexity_delta: float = Field(allow_inf_nan=False)
    proposal_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("target_universe", mode="after")
    @classmethod
    def normalize_universe(cls, value: list[str]) -> list[str]:
        normalized = [symbol.strip().upper() for symbol in value]
        if any(
            re.fullmatch(SYMBOL_PATTERN, symbol) is None
            for symbol in normalized
        ):
            raise ValueError(
                "target_universe contains an invalid US market symbol"
            )
        if len(normalized) != len(set(normalized)):
            raise ValueError("target_universe symbols must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_recursive_action(self) -> Self:
        if (
            self.proposed_strategy_id == self.parent_strategy_id
            and self.proposed_strategy_version == self.parent_strategy_version
        ):
            raise ValueError(
                "a proposal cannot overwrite its parent strategy version"
            )
        if self.primary_action_kind is ResearchActionKind.UNKNOWN_LEGACY:
            raise ValueError("AlgorithmProposalV2 requires a typed primary action")
        if len(set(self.secondary_action_kinds)) != len(self.secondary_action_kinds):
            raise ValueError("secondary action kinds must be unique")
        if self.primary_action_kind in self.secondary_action_kinds:
            raise ValueError("primary action cannot also be secondary")
        if ResearchActionKind.UNKNOWN_LEGACY in self.secondary_action_kinds:
            raise ValueError("AlgorithmProposalV2 cannot declare UNKNOWN_LEGACY")
        _require_sorted_unique(self.mechanism_tags, "mechanism_tags")
        _require_sorted_unique(
            self.predicted_failure_codes,
            "predicted_failure_codes",
        )
        for pattern in self.files_allowed_to_change:
            normalized = _proposal_pattern_root(pattern)
            if not any(
                normalized.startswith(allowed.removesuffix("**"))
                for allowed in V2_ALLOWED_PATTERNS
            ):
                raise ValueError(
                    "AlgorithmProposalV2 files_allowed_to_change exceeds "
                    "candidate_patch_policy_v2"
                )
        if canonical_hash(
            self.model_dump(mode="python", exclude={"proposal_hash"})
        ) != self.proposal_hash:
            raise ValueError("proposal_hash mismatch")
        return self


class ResearchExperimentActionV1(DomainModel):
    schema_version: Literal[
        "research_experiment_action_v1"
    ] = "research_experiment_action_v1"
    action_id: str = Field(pattern=IDENTIFIER_PATTERN)
    experiment_id: str = Field(pattern=IDENTIFIER_PATTERN)
    research_cycle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    proposal_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parent_strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parent_strategy_version: str = Field(pattern=VERSION_PATTERN)
    candidate_strategy_version: str = Field(pattern=VERSION_PATTERN)
    primary_action_kind: ResearchActionKind
    secondary_action_kinds: tuple[ResearchActionKind, ...] = Field(
        default=(),
        max_length=3,
    )
    mechanism_tags: tuple[str, ...] = Field(default=(), max_length=64)
    information_role: ExperimentInformationRole
    decision_at: datetime
    maturity_due_at: datetime
    predicted_delta_sharpe_lower: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    predicted_delta_sharpe_median: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    predicted_delta_sharpe_upper: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    predicted_failure_codes: tuple[str, ...] = Field(default=(), max_length=64)
    complexity_delta: float = Field(allow_inf_nan=False)
    candidate_artifact_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    source_artifact_hashes: tuple[str, ...] = Field(default=(), max_length=512)
    source_data_available_at: tuple[datetime, ...] = Field(
        default=(),
        max_length=512,
    )
    legacy_proposal: bool
    meta_training_permitted: bool
    idempotency_key: str = Field(pattern=IDENTIFIER_PATTERN)
    created_at: datetime
    action_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "decision_at",
        "maturity_due_at",
        "source_data_available_at",
        "created_at",
        mode="after",
    )
    @classmethod
    def validate_times(
        cls,
        value: datetime | tuple[datetime, ...],
    ) -> datetime | tuple[datetime, ...]:
        if isinstance(value, tuple):
            return tuple(require_aware_utc(item) for item in value)
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.maturity_due_at < self.decision_at:
            raise ValueError("maturity_due_at cannot precede decision_at")
        if self.created_at < self.decision_at:
            raise ValueError("action cannot be created before its decision")
        if self.created_at > self.maturity_due_at:
            raise ValueError("action must be registered before maturity_due_at")
        if len(set(self.secondary_action_kinds)) != len(self.secondary_action_kinds):
            raise ValueError("secondary action kinds must be unique")
        if self.primary_action_kind in self.secondary_action_kinds:
            raise ValueError("primary action cannot also be secondary")
        _require_sorted_unique(self.mechanism_tags, "mechanism_tags")
        _require_sorted_unique(
            self.predicted_failure_codes,
            "predicted_failure_codes",
        )
        _require_canonical_source_provenance(
            self.source_artifact_hashes,
            self.source_data_available_at,
            label="action",
        )
        if any(
            value > self.decision_at
            for value in self.source_data_available_at
        ):
            raise ValueError("action source data was unavailable at decision time")
        predictions = (
            self.predicted_delta_sharpe_lower,
            self.predicted_delta_sharpe_median,
            self.predicted_delta_sharpe_upper,
        )
        if any(value is None for value in predictions) != all(
            value is None for value in predictions
        ):
            raise ValueError("predicted Sharpe bounds must be all present or all absent")
        if predictions[0] is not None and not (
            predictions[0] <= predictions[1] <= predictions[2]  # type: ignore[operator]
        ):
            raise ValueError("predicted Sharpe bounds are not ordered")
        expected_permission = (
            self.information_role is ExperimentInformationRole.LEARNING_FORWARD
            and not self.legacy_proposal
            and self.primary_action_kind is not ResearchActionKind.UNKNOWN_LEGACY
            and self.candidate_artifact_hash is not None
        )
        if self.meta_training_permitted is not expected_permission:
            raise ValueError("meta-training permission does not match action provenance")
        if self.legacy_proposal != (
            self.primary_action_kind is ResearchActionKind.UNKNOWN_LEGACY
        ):
            raise ValueError("legacy proposal must map only to UNKNOWN_LEGACY")
        if canonical_hash(
            self.model_dump(mode="python", exclude={"action_hash"})
        ) != self.action_hash:
            raise ValueError("research experiment action hash mismatch")
        return self


class ExperimentOutcomeMaturationInputV1(DomainModel):
    schema_version: Literal[
        "experiment_outcome_maturation_input_v1"
    ] = "experiment_outcome_maturation_input_v1"
    experiment_id: str = Field(pattern=IDENTIFIER_PATTERN)
    event_kind: ExperimentOutcomeEventKind
    experiment_stage: ExperimentStage
    evaluation_window_start: datetime | None = None
    evaluation_window_end: datetime | None = None
    available_at: datetime
    maturity_status: ExperimentMaturityStatus
    technical_success: bool | None = None
    technical_failure_codes: tuple[str, ...] = Field(default=(), max_length=64)
    portfolio_delta_sharpe_point: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    portfolio_delta_sharpe_lcb: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    portfolio_delta_sharpe_ucb: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    worst_cost_delta_sharpe_lcb: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    drawdown_delta: float | None = Field(default=None, allow_inf_nan=False)
    tail_loss_delta: float | None = Field(default=None, allow_inf_nan=False)
    turnover_delta: float | None = Field(default=None, allow_inf_nan=False)
    cost_delta_bps: float | None = Field(default=None, allow_inf_nan=False)
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    source_artifact_hashes: tuple[str, ...] = Field(default=(), max_length=512)
    source_data_available_at: tuple[datetime, ...] = Field(
        default=(),
        max_length=512,
    )
    supersedes_event_id: str | None = Field(
        default=None,
        pattern=IDENTIFIER_PATTERN,
    )
    idempotency_key: str = Field(pattern=IDENTIFIER_PATTERN)
    created_at: datetime

    @field_validator(
        "evaluation_window_start",
        "evaluation_window_end",
        "available_at",
        "source_data_available_at",
        "created_at",
        mode="after",
    )
    @classmethod
    def validate_times(
        cls,
        value: datetime | tuple[datetime, ...] | None,
    ) -> datetime | tuple[datetime, ...] | None:
        if value is None:
            return None
        if isinstance(value, tuple):
            return tuple(require_aware_utc(item) for item in value)
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        _validate_outcome_values(
            maturity_status=self.maturity_status,
            technical_success=self.technical_success,
            technical_failure_codes=self.technical_failure_codes,
            economic_values=_economic_values(self),
            evaluation_window_start=self.evaluation_window_start,
            evaluation_window_end=self.evaluation_window_end,
            available_at=self.available_at,
            created_at=self.created_at,
        )
        _validate_event_semantics(
            event_kind=self.event_kind,
            maturity_status=self.maturity_status,
            technical_success=self.technical_success,
            economic_values=_economic_values(self),
            supersedes_event_id=self.supersedes_event_id,
        )
        _require_sorted_unique(
            self.technical_failure_codes,
            "technical_failure_codes",
        )
        _require_canonical_source_provenance(
            self.source_artifact_hashes,
            self.source_data_available_at,
            label="outcome input",
        )
        if any(value > self.available_at for value in self.source_data_available_at):
            raise ValueError("source data was unavailable at outcome availability")
        return self


class ExperimentOutcomeEventV1(DomainModel):
    schema_version: Literal[
        "experiment_outcome_event_v1"
    ] = "experiment_outcome_event_v1"
    event_id: str = Field(pattern=IDENTIFIER_PATTERN)
    experiment_id: str = Field(pattern=IDENTIFIER_PATTERN)
    research_cycle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    proposal_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parent_strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parent_strategy_version: str = Field(pattern=VERSION_PATTERN)
    candidate_strategy_version: str = Field(pattern=VERSION_PATTERN)
    primary_action_kind: ResearchActionKind
    secondary_action_kinds: tuple[ResearchActionKind, ...] = Field(
        default=(),
        max_length=3,
    )
    mechanism_tags: tuple[str, ...] = Field(default=(), max_length=64)
    information_role: ExperimentInformationRole
    event_kind: ExperimentOutcomeEventKind
    experiment_stage: ExperimentStage
    event_sequence: int = Field(ge=1)
    decision_at: datetime
    evaluation_window_start: datetime | None = None
    evaluation_window_end: datetime | None = None
    available_at: datetime
    maturity_due_at: datetime
    maturity_status: ExperimentMaturityStatus
    technical_success: bool | None = None
    technical_failure_codes: tuple[str, ...] = Field(default=(), max_length=64)
    portfolio_delta_sharpe_point: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    portfolio_delta_sharpe_lcb: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    portfolio_delta_sharpe_ucb: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    worst_cost_delta_sharpe_lcb: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    drawdown_delta: float | None = Field(default=None, allow_inf_nan=False)
    tail_loss_delta: float | None = Field(default=None, allow_inf_nan=False)
    turnover_delta: float | None = Field(default=None, allow_inf_nan=False)
    cost_delta_bps: float | None = Field(default=None, allow_inf_nan=False)
    complexity_delta: float = Field(allow_inf_nan=False)
    predicted_delta_sharpe_lower: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    predicted_delta_sharpe_median: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    predicted_delta_sharpe_upper: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    prediction_error: float | None = Field(default=None, allow_inf_nan=False)
    candidate_artifact_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    source_artifact_hashes: tuple[str, ...] = Field(default=(), max_length=512)
    source_data_available_at: tuple[datetime, ...] = Field(
        default=(),
        max_length=512,
    )
    eligible_for_meta_training: bool
    previous_event_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    supersedes_event_id: str | None = Field(
        default=None,
        pattern=IDENTIFIER_PATTERN,
    )
    idempotency_key: str = Field(pattern=IDENTIFIER_PATTERN)
    maturation_input_hash: str = Field(pattern=HASH_PATTERN)
    created_at: datetime
    event_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "decision_at",
        "evaluation_window_start",
        "evaluation_window_end",
        "available_at",
        "maturity_due_at",
        "source_data_available_at",
        "created_at",
        mode="after",
    )
    @classmethod
    def validate_times(
        cls,
        value: datetime | tuple[datetime, ...] | None,
    ) -> datetime | tuple[datetime, ...] | None:
        if value is None:
            return None
        if isinstance(value, tuple):
            return tuple(require_aware_utc(item) for item in value)
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.maturity_due_at < self.decision_at:
            raise ValueError("maturity_due_at cannot precede decision_at")
        if (self.event_sequence == 1) != (self.previous_event_hash is None):
            raise ValueError("event-chain predecessor does not match sequence")
        if self.supersedes_event_id == self.event_id:
            raise ValueError("an outcome event cannot supersede itself")
        _validate_outcome_values(
            maturity_status=self.maturity_status,
            technical_success=self.technical_success,
            technical_failure_codes=self.technical_failure_codes,
            economic_values=_economic_values(self),
            evaluation_window_start=self.evaluation_window_start,
            evaluation_window_end=self.evaluation_window_end,
            available_at=self.available_at,
            created_at=self.created_at,
        )
        _validate_event_semantics(
            event_kind=self.event_kind,
            maturity_status=self.maturity_status,
            technical_success=self.technical_success,
            economic_values=_economic_values(self),
            supersedes_event_id=self.supersedes_event_id,
        )
        _require_sorted_unique(
            self.technical_failure_codes,
            "technical_failure_codes",
        )
        _require_sorted_unique(self.mechanism_tags, "mechanism_tags")
        _require_canonical_source_provenance(
            self.source_artifact_hashes,
            self.source_data_available_at,
            label="outcome event",
        )
        if any(value > self.available_at for value in self.source_data_available_at):
            raise ValueError("source data was unavailable at outcome availability")
        predictions = (
            self.predicted_delta_sharpe_lower,
            self.predicted_delta_sharpe_median,
            self.predicted_delta_sharpe_upper,
        )
        if any(value is None for value in predictions) != all(
            value is None for value in predictions
        ):
            raise ValueError("predicted Sharpe bounds must be all present or all absent")
        if predictions[0] is not None and not (
            predictions[0] <= predictions[1] <= predictions[2]  # type: ignore[operator]
        ):
            raise ValueError("predicted Sharpe bounds are not ordered")
        expected_error = (
            None
            if (
                self.portfolio_delta_sharpe_point is None
                or self.predicted_delta_sharpe_median is None
            )
            else (
                self.portfolio_delta_sharpe_point
                - self.predicted_delta_sharpe_median
            )
        )
        if self.prediction_error != expected_error:
            raise ValueError("prediction_error arithmetic mismatch")
        expected_eligible = _meta_training_eligible(
            information_role=self.information_role,
            event_kind=self.event_kind,
            maturity_status=self.maturity_status,
            primary_action_kind=self.primary_action_kind,
            candidate_artifact_hash=self.candidate_artifact_hash,
            economic_values=_economic_values(self),
        )
        if self.eligible_for_meta_training is not expected_eligible:
            raise ValueError("meta-training eligibility mismatch")
        if (
            any(value is not None for value in _economic_values(self))
            and self.available_at < self.maturity_due_at
        ):
            raise ValueError("economic outcome matured before maturity_due_at")
        if canonical_hash(
            self.model_dump(mode="python", exclude={"event_hash"})
        ) != self.event_hash:
            raise ValueError("experiment outcome event hash mismatch")
        return self


class ResearchActionStatisticV1(DomainModel):
    primary_action_kind: ResearchActionKind
    included_event_count: int = Field(ge=0)
    matured_economic_outcome_count: int = Field(ge=0)
    technical_success_count: int = Field(ge=0)
    technical_failure_count: int = Field(ge=0)
    mean_portfolio_delta_sharpe: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    mean_portfolio_delta_sharpe_lcb: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    mean_prediction_error: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )


class ResearchFailureClusterV1(DomainModel):
    failure_code: str = Field(pattern=IDENTIFIER_PATTERN)
    event_count: int = Field(gt=0)
    action_kinds: tuple[ResearchActionKind, ...]


class ResearchRegimeActionStatisticV1(DomainModel):
    regime_descriptor: str = Field(pattern=IDENTIFIER_PATTERN)
    primary_action_kind: ResearchActionKind
    matured_economic_outcome_count: int = Field(ge=0)
    mean_portfolio_delta_sharpe_lcb: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )


class HistoricalExperimentAnalogV1(DomainModel):
    event_hash: str = Field(pattern=HASH_PATTERN)
    experiment_id: str = Field(pattern=IDENTIFIER_PATTERN)
    primary_action_kind: ResearchActionKind
    mechanism_tags: tuple[str, ...]
    portfolio_delta_sharpe_lcb: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    worst_cost_delta_sharpe_lcb: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    available_at: datetime

    @field_validator("available_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)


class PredictionCalibrationSummaryV1(DomainModel):
    observation_count: int = Field(ge=0)
    mean_error: float | None = Field(default=None, allow_inf_nan=False)
    mean_absolute_error: float | None = Field(default=None, allow_inf_nan=False)


class ResearchMemorySnapshotV1(DomainModel):
    schema_version: Literal[
        "research_memory_snapshot_v1"
    ] = "research_memory_snapshot_v1"
    snapshot_id: str = Field(pattern=IDENTIFIER_PATTERN)
    as_of: datetime
    data_available_cutoff: datetime
    included_event_hashes: tuple[str, ...]
    excluded_future_event_count: int = Field(ge=0)
    excluded_unmatured_event_count: int = Field(ge=0)
    excluded_oos_event_count: int = Field(ge=0)
    excluded_meta_audit_event_count: int = Field(ge=0)
    excluded_invalid_event_count: int = Field(ge=0)
    action_statistics: tuple[ResearchActionStatisticV1, ...]
    recent_failure_clusters: tuple[ResearchFailureClusterV1, ...]
    regime_action_statistics: tuple[ResearchRegimeActionStatisticV1, ...]
    nearest_historical_analogs: tuple[HistoricalExperimentAnalogV1, ...]
    prediction_calibration_summary: PredictionCalibrationSummaryV1
    snapshot_hash: str = Field(pattern=HASH_PATTERN)
    created_at: datetime

    @field_validator(
        "as_of",
        "data_available_cutoff",
        "created_at",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.data_available_cutoff > self.as_of:
            raise ValueError("memory data cutoff cannot exceed as_of")
        if self.created_at < self.as_of:
            raise ValueError("memory snapshot cannot be created before as_of")
        _require_hash_sequence(self.included_event_hashes)
        if canonical_hash(
            self.model_dump(mode="python", exclude={"snapshot_hash"})
        ) != self.snapshot_hash:
            raise ValueError("research memory snapshot hash mismatch")
        return self


def build_experiment_action(
    *,
    proposal: AlgorithmProposalV1 | AlgorithmProposalV2,
    experiment_id: str,
    research_cycle_id: str,
    challenger_id: str,
    information_role: ExperimentInformationRole,
    decision_at: datetime,
    maturity_due_at: datetime,
    candidate_artifact_hash: str | None,
    evaluation_contract_hash: str,
    source_artifact_hashes: tuple[str, ...],
    source_data_available_at: tuple[datetime, ...],
    idempotency_key: str,
    created_at: datetime,
) -> ResearchExperimentActionV1:
    normalized_source_hashes, normalized_source_available_at = (
        _canonical_source_provenance(
            source_artifact_hashes,
            source_data_available_at,
        )
    )
    if isinstance(proposal, AlgorithmProposalV2):
        interval = proposal.predicted_portfolio_delta_sharpe
        primary_action = proposal.primary_action_kind
        secondary_actions = proposal.secondary_action_kinds
        mechanism_tags = proposal.mechanism_tags
        predicted_failure_codes = proposal.predicted_failure_codes
        predictions: tuple[float | None, float | None, float | None] = (
            interval.lower,
            interval.median,
            interval.upper,
        )
        complexity_delta = proposal.complexity_delta
        legacy = False
    else:
        primary_action = ResearchActionKind.UNKNOWN_LEGACY
        secondary_actions = ()
        mechanism_tags = ()
        predicted_failure_codes = ()
        predictions = (None, None, None)
        complexity_delta = 0.0
        legacy = True
    payload = {
        "schema_version": "research_experiment_action_v1",
        "action_id": stable_id("research-experiment-action", experiment_id),
        "experiment_id": experiment_id,
        "research_cycle_id": research_cycle_id,
        "proposal_id": proposal.proposal_id,
        "challenger_id": challenger_id,
        "parent_strategy_id": proposal.parent_strategy_id,
        "parent_strategy_version": proposal.parent_strategy_version,
        "candidate_strategy_version": proposal.proposed_strategy_version,
        "primary_action_kind": primary_action,
        "secondary_action_kinds": secondary_actions,
        "mechanism_tags": mechanism_tags,
        "information_role": information_role,
        "decision_at": require_aware_utc(decision_at),
        "maturity_due_at": require_aware_utc(maturity_due_at),
        "predicted_delta_sharpe_lower": predictions[0],
        "predicted_delta_sharpe_median": predictions[1],
        "predicted_delta_sharpe_upper": predictions[2],
        "predicted_failure_codes": predicted_failure_codes,
        "complexity_delta": complexity_delta,
        "candidate_artifact_hash": candidate_artifact_hash,
        "evaluation_contract_hash": evaluation_contract_hash,
        "source_artifact_hashes": normalized_source_hashes,
        "source_data_available_at": normalized_source_available_at,
        "legacy_proposal": legacy,
        "meta_training_permitted": (
            information_role is ExperimentInformationRole.LEARNING_FORWARD
            and not legacy
            and candidate_artifact_hash is not None
        ),
        "idempotency_key": idempotency_key,
        "created_at": require_aware_utc(created_at),
    }
    return ResearchExperimentActionV1.model_validate(
        {**payload, "action_hash": canonical_hash(payload)}
    )


def build_outcome_event(
    *,
    action: ResearchExperimentActionV1,
    maturation: ExperimentOutcomeMaturationInputV1,
    previous_event: ExperimentOutcomeEventV1 | None,
) -> ExperimentOutcomeEventV1:
    if maturation.experiment_id != action.experiment_id:
        raise ValueError("maturation input belongs to another experiment")
    if maturation.evaluation_contract_hash != action.evaluation_contract_hash:
        raise ValueError("maturation evaluation contract differs from action")
    if (
        any(value is not None for value in _economic_values(maturation))
        and maturation.available_at < action.maturity_due_at
    ):
        raise ValueError("economic outcome cannot mature before maturity_due_at")
    if (
        any(value is not None for value in _economic_values(maturation))
        and (
            maturation.evaluation_window_start is None
            or maturation.evaluation_window_start
            < max(action.decision_at, action.created_at)
        )
    ):
        raise ValueError(
            "economic evaluation window predates experiment registration"
        )
    sequence = 1 if previous_event is None else previous_event.event_sequence + 1
    if (
        previous_event is not None
        and previous_event.experiment_id != action.experiment_id
    ):
        raise ValueError("previous event belongs to another experiment")
    if (
        previous_event is not None
        and maturation.created_at < previous_event.created_at
    ):
        raise ValueError("outcome event creation time cannot regress")
    if maturation.created_at < action.created_at:
        raise ValueError("outcome event cannot predate its registered action")
    if maturation.available_at < max(action.decision_at, action.created_at):
        raise ValueError("outcome availability cannot predate its registered action")
    if (
        previous_event is not None
        and maturation.available_at < previous_event.available_at
    ):
        raise ValueError("outcome availability time cannot regress")
    normalized_source_hashes, normalized_source_available_at = (
        _canonical_source_provenance(
            maturation.source_artifact_hashes,
            maturation.source_data_available_at,
        )
    )
    prediction_error = (
        None
        if (
            maturation.portfolio_delta_sharpe_point is None
            or action.predicted_delta_sharpe_median is None
        )
        else (
            maturation.portfolio_delta_sharpe_point
            - action.predicted_delta_sharpe_median
        )
    )
    payload = {
        "schema_version": "experiment_outcome_event_v1",
        "event_id": stable_id(
            "experiment-outcome-event",
            action.experiment_id,
            maturation.idempotency_key,
        ),
        "experiment_id": action.experiment_id,
        "research_cycle_id": action.research_cycle_id,
        "proposal_id": action.proposal_id,
        "challenger_id": action.challenger_id,
        "parent_strategy_id": action.parent_strategy_id,
        "parent_strategy_version": action.parent_strategy_version,
        "candidate_strategy_version": action.candidate_strategy_version,
        "primary_action_kind": action.primary_action_kind,
        "secondary_action_kinds": action.secondary_action_kinds,
        "mechanism_tags": action.mechanism_tags,
        "information_role": action.information_role,
        "event_kind": maturation.event_kind,
        "experiment_stage": maturation.experiment_stage,
        "event_sequence": sequence,
        "decision_at": action.decision_at,
        "evaluation_window_start": maturation.evaluation_window_start,
        "evaluation_window_end": maturation.evaluation_window_end,
        "available_at": maturation.available_at,
        "maturity_due_at": action.maturity_due_at,
        "maturity_status": maturation.maturity_status,
        "technical_success": maturation.technical_success,
        "technical_failure_codes": maturation.technical_failure_codes,
        "portfolio_delta_sharpe_point": (
            maturation.portfolio_delta_sharpe_point
        ),
        "portfolio_delta_sharpe_lcb": maturation.portfolio_delta_sharpe_lcb,
        "portfolio_delta_sharpe_ucb": maturation.portfolio_delta_sharpe_ucb,
        "worst_cost_delta_sharpe_lcb": (
            maturation.worst_cost_delta_sharpe_lcb
        ),
        "drawdown_delta": maturation.drawdown_delta,
        "tail_loss_delta": maturation.tail_loss_delta,
        "turnover_delta": maturation.turnover_delta,
        "cost_delta_bps": maturation.cost_delta_bps,
        "complexity_delta": action.complexity_delta,
        "predicted_delta_sharpe_lower": action.predicted_delta_sharpe_lower,
        "predicted_delta_sharpe_median": action.predicted_delta_sharpe_median,
        "predicted_delta_sharpe_upper": action.predicted_delta_sharpe_upper,
        "prediction_error": prediction_error,
        "candidate_artifact_hash": action.candidate_artifact_hash,
        "evaluation_contract_hash": maturation.evaluation_contract_hash,
        "source_artifact_hashes": normalized_source_hashes,
        "source_data_available_at": normalized_source_available_at,
        "eligible_for_meta_training": _meta_training_eligible(
            information_role=action.information_role,
            event_kind=maturation.event_kind,
            maturity_status=maturation.maturity_status,
            primary_action_kind=action.primary_action_kind,
            candidate_artifact_hash=action.candidate_artifact_hash,
            economic_values=_economic_values(maturation),
        ),
        "previous_event_hash": (
            None if previous_event is None else previous_event.event_hash
        ),
        "supersedes_event_id": maturation.supersedes_event_id,
        "idempotency_key": maturation.idempotency_key,
        "maturation_input_hash": canonical_hash(maturation),
        "created_at": maturation.created_at,
    }
    return ExperimentOutcomeEventV1.model_validate(
        {**payload, "event_hash": canonical_hash(payload)}
    )


def build_research_memory_snapshot_from_verified_events(
    *,
    events: tuple[ExperimentOutcomeEventV1, ...],
    as_of: datetime,
    data_available_cutoff: datetime,
    created_at: datetime,
) -> ResearchMemorySnapshotV1:
    """Build aggregate memory from a host-verified append-only event prefix.

    Production callers must obtain ``events`` from the trusted repository.
    The function is pure so deterministic replay can verify the same prefix.
    """

    instant = require_aware_utc(as_of)
    cutoff = require_aware_utc(data_available_cutoff)
    created = require_aware_utc(created_at)
    if cutoff > instant:
        raise ValueError("memory data cutoff cannot exceed as_of")
    prefix = tuple(event for event in events if event.created_at <= instant)
    superseded_ids = {
        event.supersedes_event_id
        for event in prefix
        if (
            event.supersedes_event_id is not None
            and event.available_at <= cutoff
        )
    }
    counts = {
        "future": 0,
        "unmatured": 0,
        "oos": 0,
        "meta_audit": 0,
        "invalid": 0,
    }
    included: list[ExperimentOutcomeEventV1] = []
    for event in prefix:
        if event.available_at > cutoff:
            counts["future"] += 1
        elif event.information_role is ExperimentInformationRole.PROMOTION_OOS:
            counts["oos"] += 1
        elif event.information_role is ExperimentInformationRole.META_AUDIT:
            counts["meta_audit"] += 1
        elif event.maturity_status is ExperimentMaturityStatus.PENDING:
            counts["unmatured"] += 1
        elif (
            event.maturity_status
            in {
                ExperimentMaturityStatus.CENSORED,
                ExperimentMaturityStatus.INVALIDATED,
                ExperimentMaturityStatus.SUPERSEDED,
            }
            or event.event_id in superseded_ids
        ):
            counts["invalid"] += 1
        else:
            included.append(event)
    included.sort(
        key=lambda item: (
            item.available_at,
            item.experiment_id,
            item.event_sequence,
            item.event_id,
        )
    )
    statistics = _action_statistics(tuple(included))
    failure_clusters = _failure_clusters(tuple(included))
    economic_events = tuple(
        item for item in included if item.eligible_for_meta_training
    )
    analogs = tuple(
        HistoricalExperimentAnalogV1(
            event_hash=item.event_hash,
            experiment_id=item.experiment_id,
            primary_action_kind=item.primary_action_kind,
            mechanism_tags=item.mechanism_tags,
            portfolio_delta_sharpe_lcb=item.portfolio_delta_sharpe_lcb,
            worst_cost_delta_sharpe_lcb=item.worst_cost_delta_sharpe_lcb,
            available_at=item.available_at,
        )
        for item in sorted(
            economic_events,
            key=lambda value: (
                value.available_at,
                value.experiment_id,
                value.event_sequence,
            ),
            reverse=True,
        )
    )
    prediction_errors = tuple(
        item.prediction_error
        for item in economic_events
        if item.prediction_error is not None
    )
    calibration = PredictionCalibrationSummaryV1(
        observation_count=len(prediction_errors),
        mean_error=(
            None if not prediction_errors else fmean(prediction_errors)
        ),
        mean_absolute_error=(
            None
            if not prediction_errors
            else fmean(abs(value) for value in prediction_errors)
        ),
    )
    included_hashes = tuple(item.event_hash for item in included)
    snapshot_id = stable_id(
        "research-memory-snapshot",
        instant,
        cutoff,
        included_hashes,
        created,
    )
    payload = {
        "schema_version": "research_memory_snapshot_v1",
        "snapshot_id": snapshot_id,
        "as_of": instant,
        "data_available_cutoff": cutoff,
        "included_event_hashes": included_hashes,
        "excluded_future_event_count": counts["future"],
        "excluded_unmatured_event_count": counts["unmatured"],
        "excluded_oos_event_count": counts["oos"],
        "excluded_meta_audit_event_count": counts["meta_audit"],
        "excluded_invalid_event_count": counts["invalid"],
        "action_statistics": statistics,
        "recent_failure_clusters": failure_clusters,
        # A typed regime descriptor is deliberately deferred to PR2. No
        # free-form tag is reinterpreted as a regime in this trusted ledger.
        "regime_action_statistics": (),
        "nearest_historical_analogs": analogs,
        "prediction_calibration_summary": calibration,
        "created_at": created,
    }
    return ResearchMemorySnapshotV1.model_validate(
        {**payload, "snapshot_hash": canonical_hash(payload)}
    )


def _action_statistics(
    events: tuple[ExperimentOutcomeEventV1, ...],
) -> tuple[ResearchActionStatisticV1, ...]:
    grouped: dict[
        ResearchActionKind,
        list[ExperimentOutcomeEventV1],
    ] = defaultdict(list)
    for event in events:
        grouped[event.primary_action_kind].append(event)
    result: list[ResearchActionStatisticV1] = []
    for action in sorted(grouped, key=lambda item: item.value):
        items = grouped[action]
        economic = [item for item in items if item.eligible_for_meta_training]
        points = [
            item.portfolio_delta_sharpe_point
            for item in economic
            if item.portfolio_delta_sharpe_point is not None
        ]
        lcbs = [
            item.portfolio_delta_sharpe_lcb
            for item in economic
            if item.portfolio_delta_sharpe_lcb is not None
        ]
        errors = [
            item.prediction_error
            for item in economic
            if item.prediction_error is not None
        ]
        result.append(
            ResearchActionStatisticV1(
                primary_action_kind=action,
                included_event_count=len(items),
                matured_economic_outcome_count=len(economic),
                technical_success_count=sum(
                    item.technical_success is True for item in items
                ),
                technical_failure_count=sum(
                    item.technical_success is False for item in items
                ),
                mean_portfolio_delta_sharpe=(
                    None if not points else fmean(points)
                ),
                mean_portfolio_delta_sharpe_lcb=(
                    None if not lcbs else fmean(lcbs)
                ),
                mean_prediction_error=(
                    None if not errors else fmean(errors)
                ),
            )
        )
    return tuple(result)


def _failure_clusters(
    events: tuple[ExperimentOutcomeEventV1, ...],
) -> tuple[ResearchFailureClusterV1, ...]:
    counts: Counter[str] = Counter()
    action_kinds: dict[str, set[ResearchActionKind]] = defaultdict(set)
    for event in events:
        for code in event.technical_failure_codes:
            counts[code] += 1
            action_kinds[code].add(event.primary_action_kind)
    return tuple(
        ResearchFailureClusterV1(
            failure_code=code,
            event_count=count,
            action_kinds=tuple(
                sorted(action_kinds[code], key=lambda item: item.value)
            ),
        )
        for code, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    )


def _meta_training_eligible(
    *,
    information_role: ExperimentInformationRole,
    event_kind: ExperimentOutcomeEventKind,
    maturity_status: ExperimentMaturityStatus,
    primary_action_kind: ResearchActionKind,
    candidate_artifact_hash: str | None,
    economic_values: tuple[float | None, ...],
) -> bool:
    return (
        information_role is ExperimentInformationRole.LEARNING_FORWARD
        and event_kind
        in {
            ExperimentOutcomeEventKind.ECONOMIC_OUTCOME_MATURED,
            ExperimentOutcomeEventKind.OUTCOME_CORRECTED,
        }
        and maturity_status is ExperimentMaturityStatus.MATURED
        and primary_action_kind is not ResearchActionKind.UNKNOWN_LEGACY
        and candidate_artifact_hash is not None
        and any(value is not None for value in economic_values)
    )


def _validate_event_semantics(
    *,
    event_kind: ExperimentOutcomeEventKind,
    maturity_status: ExperimentMaturityStatus,
    technical_success: bool | None,
    economic_values: tuple[float | None, ...],
    supersedes_event_id: str | None,
) -> None:
    has_economic_values = any(value is not None for value in economic_values)
    if event_kind is ExperimentOutcomeEventKind.EXPERIMENT_REGISTERED:
        if (
            maturity_status is not ExperimentMaturityStatus.PENDING
            or technical_success is not None
            or has_economic_values
        ):
            raise ValueError(
                "EXPERIMENT_REGISTERED must be a pending non-outcome event"
            )
    elif event_kind is ExperimentOutcomeEventKind.TECHNICAL_OUTCOME_RECORDED:
        if (
            maturity_status is not ExperimentMaturityStatus.MATURED
            or technical_success is None
            or has_economic_values
        ):
            raise ValueError(
                "TECHNICAL_OUTCOME_RECORDED requires a mature technical outcome"
            )
    elif event_kind is ExperimentOutcomeEventKind.ECONOMIC_OUTCOME_MATURED:
        if (
            maturity_status is not ExperimentMaturityStatus.MATURED
            or not has_economic_values
        ):
            raise ValueError(
                "ECONOMIC_OUTCOME_MATURED requires mature economic values"
            )
    elif event_kind is ExperimentOutcomeEventKind.OUTCOME_CENSORED:
        if maturity_status is not ExperimentMaturityStatus.CENSORED:
            raise ValueError("OUTCOME_CENSORED requires CENSORED status")
    elif (
        event_kind is ExperimentOutcomeEventKind.OUTCOME_INVALIDATED
        and maturity_status is not ExperimentMaturityStatus.INVALIDATED
    ):
        raise ValueError("OUTCOME_INVALIDATED requires INVALIDATED status")

    is_correction = event_kind is ExperimentOutcomeEventKind.OUTCOME_CORRECTED
    if is_correction != (supersedes_event_id is not None):
        raise ValueError(
            "only OUTCOME_CORRECTED may declare supersedes_event_id"
        )
    if (
        is_correction
        and maturity_status is ExperimentMaturityStatus.PENDING
    ):
        raise ValueError("OUTCOME_CORRECTED cannot remain PENDING")
    if (
        maturity_status is ExperimentMaturityStatus.SUPERSEDED
        and not is_correction
    ):
        raise ValueError("SUPERSEDED status requires a correction event")


def _validate_outcome_values(
    *,
    maturity_status: ExperimentMaturityStatus,
    technical_success: bool | None,
    technical_failure_codes: tuple[str, ...],
    economic_values: tuple[float | None, ...],
    evaluation_window_start: datetime | None,
    evaluation_window_end: datetime | None,
    available_at: datetime,
    created_at: datetime,
) -> None:
    if created_at < available_at:
        raise ValueError("outcome cannot be created before it is available")
    if (evaluation_window_start is None) != (evaluation_window_end is None):
        raise ValueError("evaluation window bounds must be both present or absent")
    if (
        evaluation_window_start is not None
        and evaluation_window_end is not None
        and (
            evaluation_window_end < evaluation_window_start
            or evaluation_window_end > available_at
        )
    ):
        raise ValueError("evaluation window is invalid at outcome availability")
    if maturity_status is not ExperimentMaturityStatus.MATURED and any(
        value is not None for value in economic_values
    ):
        raise ValueError("economic metrics must remain None until MATURED")
    if any(value is not None for value in economic_values) and (
        evaluation_window_start is None
        or evaluation_window_end is None
    ):
        raise ValueError("economic metrics require an evaluation window")
    if technical_success is True and technical_failure_codes:
        raise ValueError("technical success cannot carry failure codes")
    if technical_success is False and not technical_failure_codes:
        raise ValueError("technical failure requires a failure code")
    if technical_success is None and technical_failure_codes:
        raise ValueError("technical failure codes require a technical outcome")


def _economic_values(
    value: ExperimentOutcomeMaturationInputV1 | ExperimentOutcomeEventV1,
) -> tuple[float | None, ...]:
    return (
        value.portfolio_delta_sharpe_point,
        value.portfolio_delta_sharpe_lcb,
        value.portfolio_delta_sharpe_ucb,
        value.worst_cost_delta_sharpe_lcb,
        value.drawdown_delta,
        value.tail_loss_delta,
        value.turnover_delta,
        value.cost_delta_bps,
    )


def _canonical_source_provenance(
    source_artifact_hashes: tuple[str, ...],
    source_data_available_at: tuple[datetime, ...],
) -> tuple[tuple[str, ...], tuple[datetime, ...]]:
    if len(source_artifact_hashes) != len(source_data_available_at):
        raise ValueError(
            "every source artifact hash requires an availability time"
        )
    paired = tuple(
        sorted(
            (
                (source_hash, require_aware_utc(available_at))
                for source_hash, available_at in zip(
                    source_artifact_hashes,
                    source_data_available_at,
                    strict=True,
                )
            ),
            key=lambda item: (item[1], item[0]),
        )
    )
    return (
        tuple(source_hash for source_hash, _ in paired),
        tuple(available_at for _, available_at in paired),
    )


def _require_canonical_source_provenance(
    source_artifact_hashes: tuple[str, ...],
    source_data_available_at: tuple[datetime, ...],
    *,
    label: str,
) -> None:
    _require_hashes(source_artifact_hashes)
    if len(source_artifact_hashes) != len(source_data_available_at):
        raise ValueError(
            "every source artifact hash requires an availability time"
        )
    paired = tuple(
        zip(
            source_artifact_hashes,
            source_data_available_at,
            strict=True,
        )
    )
    if tuple(sorted(paired, key=lambda item: (item[1], item[0]))) != paired:
        raise ValueError(
            f"{label} source provenance pairs must be sorted"
        )


def _require_sorted_unique(values: tuple[object, ...], field_name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must be unique")
    if tuple(sorted(values, key=str)) != values:
        raise ValueError(f"{field_name} must be sorted")


def _require_hashes(values: tuple[str, ...]) -> None:
    if len(set(values)) != len(values):
        raise ValueError("artifact hashes must be unique")
    _require_hash_sequence(values)


def _require_hash_sequence(values: tuple[str, ...]) -> None:
    if len(set(values)) != len(values):
        raise ValueError("artifact hashes must be unique")
    if any(
        len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
        for value in values
    ):
        raise ValueError("artifact hashes must be lowercase SHA-256 values")


def _proposal_pattern_root(pattern: str) -> str:
    normalized = pattern.replace("\\", "/").strip()
    if (
        not normalized
        or normalized.startswith("/")
        or ":" in normalized.split("/", maxsplit=1)[0]
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise ValueError("unsafe AlgorithmProposalV2 change pattern")
    wildcard_at = min(
        (
            index
            for token in ("*", "?", "[")
            if (index := normalized.find(token)) >= 0
        ),
        default=len(normalized),
    )
    return normalized[:wildcard_at]
