from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.research.contracts import HASH_PATTERN, IDENTIFIER_PATTERN

BASE_VARIANT_ID = "BASE"


class KnownFactorReturnV1(DomainModel):
    factor_id: str = Field(pattern=IDENTIFIER_PATTERN)
    return_value: float

    @field_validator("return_value", mode="after")
    @classmethod
    def validate_finite_return(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("factor return must be finite")
        return value


class FalsificationEvaluationContractV1(DomainModel):
    """Versioned thresholds used by every host-owned falsification evaluator."""

    schema_version: str = Field(default="falsification_evaluation_contract_v1")
    contract_version: str = Field(pattern=IDENTIFIER_PATTERN)
    minimum_observation_count: int = Field(gt=0)
    minimum_session_count: int = Field(gt=0)
    maximum_source_age_seconds: int = Field(gt=0)
    minimum_universe_coverage_ratio: float = Field(ge=0, le=1)
    minimum_non_survivor_coverage_ratio: float = Field(ge=0, le=1)
    minimum_variant_session_coverage_ratio: float = Field(gt=0, le=1)
    minimum_base_mean_net_return: float
    maximum_parameter_relative_deviation: float = Field(ge=0)
    minimum_neighborhood_edge_ratio: float
    minimum_neighborhood_pass_fraction: float = Field(ge=0, le=1)
    maximum_placebo_edge_ratio: float
    maximum_single_symbol_positive_edge_share: float = Field(ge=0, le=1)
    maximum_single_month_positive_edge_share: float = Field(ge=0, le=1)
    top_trade_count: int = Field(gt=0)
    minimum_top_trades_removed_edge_ratio: float
    cost_stress_multipliers: tuple[float, float, float]
    minimum_cost_stress_mean_net_return: float
    delay_stress_multiplier: float = Field(ge=1)
    minimum_delay_stress_mean_net_return: float
    spread_stress_multiplier: float = Field(ge=1)
    minimum_spread_stress_mean_net_return: float
    basis_points_per_unit_return: float = Field(gt=0)
    maximum_adv_participation_ratio: float = Field(gt=0, le=1)
    minimum_capacity_pass_fraction: float = Field(ge=0, le=1)
    minimum_market_neutral_edge_ratio: float
    minimum_sector_neutral_edge_ratio: float
    minimum_known_factor_neutral_edge_ratio: float
    regression_variance_epsilon: float = Field(gt=0)
    minimum_regime_observations: int = Field(gt=0)
    minimum_regime_pass_fraction: float = Field(ge=0, le=1)
    minimum_regime_mean_net_return: float
    minimum_ablation_edge_ratio: float
    minimum_ablation_pass_fraction: float = Field(ge=0, le=1)
    numeric_tolerance: float = Field(gt=0)

    @field_validator(
        "minimum_base_mean_net_return",
        "maximum_parameter_relative_deviation",
        "minimum_neighborhood_edge_ratio",
        "maximum_placebo_edge_ratio",
        "minimum_top_trades_removed_edge_ratio",
        "minimum_cost_stress_mean_net_return",
        "minimum_delay_stress_mean_net_return",
        "minimum_spread_stress_mean_net_return",
        "minimum_market_neutral_edge_ratio",
        "minimum_sector_neutral_edge_ratio",
        "minimum_known_factor_neutral_edge_ratio",
        "minimum_regime_mean_net_return",
        "minimum_ablation_edge_ratio",
        "basis_points_per_unit_return",
        "maximum_adv_participation_ratio",
        "regression_variance_epsilon",
        "numeric_tolerance",
        mode="after",
    )
    @classmethod
    def validate_finite_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("evaluation threshold must be finite")
        return value

    @field_validator("cost_stress_multipliers", mode="after")
    @classmethod
    def validate_cost_multipliers(
        cls,
        value: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        if any(not math.isfinite(item) or item < 1 for item in value):
            raise ValueError("cost stress multipliers must be finite and at least one")
        if value != tuple(sorted(set(value))):
            raise ValueError("cost stress multipliers must be unique and increasing")
        if value[0] != 1:
            raise ValueError("cost stress multipliers must start at one")
        return value


class CandidateEvaluationObservationV1(DomainModel):
    """One host-produced, point-in-time candidate evaluation observation."""

    observation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    decision_time: datetime
    signal_data_cutoff: datetime
    available_at: datetime
    source_event_time: datetime
    outcome_available_at: datetime
    constituent_membership_available_at: datetime
    constituent_valid_from: datetime
    constituent_valid_until: datetime | None = None
    revision_available_at: datetime
    source_revision: int = Field(ge=0)
    revision_was_known_at_cutoff: bool
    instrument_id: str = Field(pattern=IDENTIFIER_PATTERN)
    instrument_is_non_survivor: bool
    trade_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_score: float
    candidate_target: float = Field(ge=0, le=1)
    candidate_return: float
    baseline_return: float
    modeled_cost: float = Field(ge=0)
    modeled_spread_bps: float = Field(ge=0)
    modeled_delay_bps: float = Field(ge=0)
    adv_usd: float = Field(gt=0)
    capacity_used_usd: float = Field(ge=0)
    market_return: float
    sector_return: float
    known_factor_returns: tuple[KnownFactorReturnV1, ...] = Field(min_length=1)
    regime: str = Field(pattern=IDENTIFIER_PATTERN)
    parameter_neighborhood_id: str = Field(pattern=IDENTIFIER_PATTERN)
    data_ablation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    date_shift_id: str = Field(pattern=IDENTIFIER_PATTERN)
    inversion_id: str = Field(pattern=IDENTIFIER_PATTERN)
    shuffle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_hashes: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "decision_time",
        "signal_data_cutoff",
        "available_at",
        "source_event_time",
        "outcome_available_at",
        "constituent_membership_available_at",
        "constituent_valid_from",
        "constituent_valid_until",
        "revision_available_at",
        mode="after",
    )
    @classmethod
    def validate_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware_utc(value)

    @field_validator(
        "candidate_score",
        "candidate_target",
        "candidate_return",
        "baseline_return",
        "modeled_cost",
        "modeled_spread_bps",
        "modeled_delay_bps",
        "adv_usd",
        "capacity_used_usd",
        "market_return",
        "sector_return",
        mode="after",
    )
    @classmethod
    def validate_finite_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("evaluation observation numbers must be finite")
        return value

    @field_validator("source_hashes", mode="after")
    @classmethod
    def validate_source_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            len(item) != 64
            or item.lower() != item
            or any(character not in "0123456789abcdef" for character in item)
            for item in value
        ):
            raise ValueError("source hash must be lowercase SHA-256")
        if value != tuple(sorted(set(value))):
            raise ValueError("source hashes must be unique and sorted")
        return value

    @field_validator("known_factor_returns", mode="after")
    @classmethod
    def validate_known_factors(
        cls,
        value: tuple[KnownFactorReturnV1, ...],
    ) -> tuple[KnownFactorReturnV1, ...]:
        factor_ids = tuple(item.factor_id for item in value)
        if factor_ids != tuple(sorted(set(factor_ids))):
            raise ValueError("known factor returns must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_point_in_time_contract(self) -> Self:
        if self.signal_data_cutoff > self.decision_time:
            raise ValueError("signal_data_cutoff must not exceed decision_time")
        if self.available_at > self.signal_data_cutoff:
            raise ValueError("future-available source cannot enter an evaluation row")
        if self.source_event_time > self.signal_data_cutoff:
            raise ValueError("future source event cannot enter an evaluation row")
        if self.source_event_time > self.available_at:
            raise ValueError("source_event_time must not follow available_at")
        if self.outcome_available_at <= self.decision_time:
            raise ValueError("outcome must become available after decision_time")
        if self.constituent_membership_available_at > self.signal_data_cutoff:
            raise ValueError("constituent membership was unavailable at cutoff")
        if self.revision_available_at > self.signal_data_cutoff:
            raise ValueError("source revision was unavailable at cutoff")
        if not self.revision_was_known_at_cutoff:
            raise ValueError("source revision must be point-in-time known")
        if self.constituent_valid_from > self.decision_time:
            raise ValueError("instrument was not yet a valid constituent")
        if (
            self.constituent_valid_until is not None
            and self.constituent_valid_until < self.decision_time
        ):
            raise ValueError("instrument was no longer a valid constituent")
        if self.instrument_is_non_survivor and self.constituent_valid_until is None:
            raise ValueError("non-survivor observation requires a validity end")
        if self.candidate_return <= -1 or self.baseline_return <= -1:
            raise ValueError("simple return must be greater than negative one")
        return self

    @property
    def variant_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.parameter_neighborhood_id,
            self.data_ablation_id,
            self.date_shift_id,
            self.inversion_id,
            self.shuffle_id,
        )

    @property
    def unique_key(self) -> tuple[datetime, str, str, str, str, str, str]:
        return (self.decision_time, self.instrument_id, *self.variant_key)

    @property
    def is_base(self) -> bool:
        return all(item == BASE_VARIANT_ID for item in self.variant_key)


class CandidateEvaluationTraceV1(DomainModel):
    """Immutable host-owned trace consumed by deterministic falsification tests."""

    schema_version: str = Field(default="candidate_evaluation_trace_v1")
    trace_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    data_manifest_hash: str = Field(pattern=HASH_PATTERN)
    evaluation_contract: FalsificationEvaluationContractV1
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    eligible_instrument_count: int = Field(gt=0)
    eligible_non_survivor_count: int = Field(ge=0)
    observations: tuple[CandidateEvaluationObservationV1, ...] = Field(min_length=1)
    created_at: datetime
    trace_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_trace(self) -> Self:
        contract = self.evaluation_contract
        if canonical_hash(contract) != self.evaluation_contract_hash:
            raise ValueError("evaluation contract hash mismatch")
        if self.eligible_non_survivor_count > self.eligible_instrument_count:
            raise ValueError("eligible non-survivor count exceeds universe")
        if len(self.observations) < contract.minimum_observation_count:
            raise ValueError("evaluation trace has too few observations")
        observation_ids = tuple(row.observation_id for row in self.observations)
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("evaluation observation IDs must be unique")
        unique_keys = tuple(row.unique_key for row in self.observations)
        if len(unique_keys) != len(set(unique_keys)):
            raise ValueError("evaluation observation keys must be unique")
        ordered = tuple(sorted(self.observations, key=_observation_sort_key))
        if self.observations != ordered:
            raise ValueError("evaluation observations must be canonically sorted")
        if self.created_at < max(row.outcome_available_at for row in self.observations):
            raise ValueError("trace cannot be created before its outcomes are available")
        base_rows = tuple(row for row in self.observations if row.is_base)
        base_sessions = {row.decision_time for row in base_rows}
        if len(base_sessions) < contract.minimum_session_count:
            raise ValueError("evaluation trace has too few base sessions")
        for row in self.observations:
            age_seconds = (row.decision_time - row.available_at).total_seconds()
            if age_seconds > contract.maximum_source_age_seconds:
                raise ValueError("stale source cannot enter an evaluation row")
        grouped: dict[
            tuple[datetime, tuple[str, str, str, str, str]],
            list[CandidateEvaluationObservationV1],
        ] = defaultdict(list)
        for row in self.observations:
            grouped[(row.decision_time, row.variant_key)].append(row)
        for rows in grouped.values():
            target = sum(row.candidate_target for row in rows)
            if target > 1 + contract.numeric_tolerance:
                raise ValueError("candidate trace uses leverage")
            first = rows[0]
            market_state = (
                first.market_return,
                first.sector_return,
                first.known_factor_returns,
                first.regime,
            )
            if any(
                (
                    row.market_return,
                    row.sector_return,
                    row.known_factor_returns,
                    row.regime,
                )
                != market_state
                for row in rows[1:]
            ):
                raise ValueError("market context must be consistent within a decision")
        payload = self.model_dump(mode="python", exclude={"trace_hash"})
        if canonical_hash(payload) != self.trace_hash:
            raise ValueError("candidate evaluation trace hash mismatch")
        return self


def build_candidate_evaluation_trace(
    *,
    trace_id: str,
    challenger_id: str,
    candidate_artifact_hash: str,
    data_manifest_hash: str,
    evaluation_contract: FalsificationEvaluationContractV1,
    eligible_instrument_count: int,
    eligible_non_survivor_count: int,
    observations: tuple[CandidateEvaluationObservationV1, ...],
    created_at: datetime,
) -> CandidateEvaluationTraceV1:
    ordered = tuple(sorted(observations, key=_observation_sort_key))
    payload = {
        "schema_version": "candidate_evaluation_trace_v1",
        "trace_id": trace_id,
        "challenger_id": challenger_id,
        "candidate_artifact_hash": candidate_artifact_hash,
        "data_manifest_hash": data_manifest_hash,
        "evaluation_contract": evaluation_contract,
        "evaluation_contract_hash": canonical_hash(evaluation_contract),
        "eligible_instrument_count": eligible_instrument_count,
        "eligible_non_survivor_count": eligible_non_survivor_count,
        "observations": ordered,
        "created_at": require_aware_utc(created_at),
    }
    return CandidateEvaluationTraceV1.model_validate(
        {
            **payload,
            "trace_hash": canonical_hash(payload),
        }
    )


def _observation_sort_key(
    row: CandidateEvaluationObservationV1,
) -> tuple[datetime, str, str, str, str, str, str]:
    return row.unique_key
