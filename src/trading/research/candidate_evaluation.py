from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.research.candidate_abi import (
    CandidateDecisionRequestV1,
    CandidateDecisionResponseV1,
    CandidateExecutor,
)
from trading.research.contracts import HASH_PATTERN, IDENTIFIER_PATTERN
from trading.research.evaluation_contracts import (
    CandidateEvaluationObservationV1,
    CandidateEvaluationTraceV1,
    FalsificationEvaluationContractV1,
    KnownFactorReturnV1,
    build_candidate_evaluation_trace,
)
from trading.research.replay import DeterministicReplayArtifactV1


class CandidateOutcomeV1(DomainModel):
    """Trusted future outcome retained outside the candidate input contract."""

    symbol: str
    trade_id: str = Field(pattern=IDENTIFIER_PATTERN)
    forward_return: float = Field(gt=-1)
    baseline_current_weight: float = Field(ge=0, le=1)
    baseline_target_weight: float = Field(ge=0, le=1)
    commission_bps: float = Field(ge=0)
    spread_bps: float = Field(ge=0)
    delay_bps: float = Field(ge=0)
    adv_usd: float = Field(gt=0)
    market_return: float
    sector_return: float
    known_factor_returns: tuple[KnownFactorReturnV1, ...] = Field(min_length=1)
    regime: str = Field(pattern=IDENTIFIER_PATTERN)
    outcome_available_at: datetime

    @field_validator("outcome_available_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator(
        "forward_return",
        "baseline_current_weight",
        "baseline_target_weight",
        "commission_bps",
        "spread_bps",
        "delay_bps",
        "adv_usd",
        "market_return",
        "sector_return",
        mode="after",
    )
    @classmethod
    def validate_finite_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("candidate outcome numbers must be finite")
        return value

    @field_validator("known_factor_returns", mode="after")
    @classmethod
    def validate_known_factors(
        cls,
        value: tuple[KnownFactorReturnV1, ...],
    ) -> tuple[KnownFactorReturnV1, ...]:
        factor_ids = tuple(item.factor_id for item in value)
        if factor_ids != tuple(sorted(set(factor_ids))):
            raise ValueError("known factors must be unique and sorted")
        return value


class CandidateEvaluationScenarioV1(DomainModel):
    """One PIT decision request plus outcomes hidden from the candidate."""

    schema_version: str = Field(default="candidate_evaluation_scenario_v1")
    scenario_id: str = Field(pattern=IDENTIFIER_PATTERN)
    request: CandidateDecisionRequestV1
    outcomes: tuple[CandidateOutcomeV1, ...] = Field(min_length=1)
    evaluation_nav_usd: float = Field(gt=0)
    scenario_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("evaluation_nav_usd", mode="after")
    @classmethod
    def validate_nav(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("evaluation NAV must be finite")
        return value

    @field_validator("outcomes", mode="after")
    @classmethod
    def validate_outcomes(
        cls,
        value: tuple[CandidateOutcomeV1, ...],
    ) -> tuple[CandidateOutcomeV1, ...]:
        symbols = tuple(item.symbol for item in value)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("candidate outcomes must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_scenario(self) -> Self:
        input_symbols = tuple(item.symbol for item in self.request.instruments)
        output_symbols = tuple(item.symbol for item in self.outcomes)
        if input_symbols != output_symbols:
            raise ValueError("outcome universe differs from candidate input")
        if any(
            item.outcome_available_at <= self.request.decision_time
            for item in self.outcomes
        ):
            raise ValueError("evaluation outcome was available at decision time")
        baseline_gross = sum(item.baseline_target_weight for item in self.outcomes)
        baseline_current_gross = sum(
            item.baseline_current_weight for item in self.outcomes
        )
        tolerance = self.request.constraints.numeric_tolerance
        if baseline_gross > 1 + tolerance or baseline_current_gross > 1 + tolerance:
            raise ValueError("baseline outcome uses leverage")
        market_context = (
            self.outcomes[0].market_return,
            self.outcomes[0].sector_return,
            self.outcomes[0].known_factor_returns,
            self.outcomes[0].regime,
            self.outcomes[0].outcome_available_at,
        )
        if any(
            (
                item.market_return,
                item.sector_return,
                item.known_factor_returns,
                item.regime,
                item.outcome_available_at,
            )
            != market_context
            for item in self.outcomes[1:]
        ):
            raise ValueError("scenario outcomes do not share market context")
        payload = self.model_dump(mode="python", exclude={"scenario_hash"})
        if canonical_hash(payload) != self.scenario_hash:
            raise ValueError("candidate evaluation scenario hash mismatch")
        return self


class CandidateEvaluationDatasetV1(DomainModel):
    schema_version: str = Field(default="candidate_evaluation_dataset_v1")
    dataset_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    source_data_manifest_hash: str = Field(pattern=HASH_PATTERN)
    eligible_instrument_count: int = Field(gt=0)
    eligible_non_survivor_count: int = Field(ge=0)
    scenarios: tuple[CandidateEvaluationScenarioV1, ...] = Field(min_length=1)
    dataset_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("scenarios", mode="after")
    @classmethod
    def validate_scenarios(
        cls,
        value: tuple[CandidateEvaluationScenarioV1, ...],
    ) -> tuple[CandidateEvaluationScenarioV1, ...]:
        keys = tuple(
            (
                item.request.decision_time,
                item.request.variant.key,
                item.scenario_id,
            )
            for item in value
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("candidate scenarios must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if self.eligible_non_survivor_count > self.eligible_instrument_count:
            raise ValueError("eligible non-survivor count exceeds universe")
        for scenario in self.scenarios:
            request = scenario.request
            if (
                request.challenger_id != self.challenger_id
                or request.candidate_artifact_hash != self.candidate_artifact_hash
                or request.source_data_manifest_hash != self.source_data_manifest_hash
            ):
                raise ValueError("evaluation scenario binding mismatch")
        payload = self.model_dump(mode="python", exclude={"dataset_hash"})
        if canonical_hash(payload) != self.dataset_hash:
            raise ValueError("candidate evaluation dataset hash mismatch")
        return self


@dataclass(frozen=True, slots=True)
class CandidateEvaluationResult:
    trace: CandidateEvaluationTraceV1
    replay: DeterministicReplayArtifactV1


@dataclass(frozen=True, slots=True)
class CandidateExecutionReplayResult:
    responses: tuple[CandidateDecisionResponseV1, ...]
    replay: DeterministicReplayArtifactV1


class CandidateEvaluationError(RuntimeError):
    pass


def build_candidate_evaluation_scenario(
    *,
    scenario_id: str,
    request: CandidateDecisionRequestV1,
    outcomes: tuple[CandidateOutcomeV1, ...],
    evaluation_nav_usd: float,
) -> CandidateEvaluationScenarioV1:
    ordered = tuple(sorted(outcomes, key=lambda item: item.symbol))
    payload = {
        "schema_version": "candidate_evaluation_scenario_v1",
        "scenario_id": scenario_id,
        "request": request,
        "outcomes": ordered,
        "evaluation_nav_usd": float(evaluation_nav_usd),
    }
    return CandidateEvaluationScenarioV1.model_validate(
        {**payload, "scenario_hash": canonical_hash(payload)}
    )


def build_candidate_evaluation_dataset(
    *,
    dataset_id: str,
    challenger_id: str,
    candidate_artifact_hash: str,
    source_data_manifest_hash: str,
    eligible_instrument_count: int,
    eligible_non_survivor_count: int,
    scenarios: tuple[CandidateEvaluationScenarioV1, ...],
) -> CandidateEvaluationDatasetV1:
    ordered = tuple(
        sorted(
            scenarios,
            key=lambda item: (
                item.request.decision_time,
                item.request.variant.key,
                item.scenario_id,
            ),
        )
    )
    payload = {
        "schema_version": "candidate_evaluation_dataset_v1",
        "dataset_id": dataset_id,
        "challenger_id": challenger_id,
        "candidate_artifact_hash": candidate_artifact_hash,
        "source_data_manifest_hash": source_data_manifest_hash,
        "eligible_instrument_count": eligible_instrument_count,
        "eligible_non_survivor_count": eligible_non_survivor_count,
        "scenarios": ordered,
    }
    return CandidateEvaluationDatasetV1.model_validate(
        {**payload, "dataset_hash": canonical_hash(payload)}
    )


def evaluate_candidate_twice(
    *,
    dataset: CandidateEvaluationDatasetV1,
    executor: CandidateExecutor,
    replay_executor: CandidateExecutor | None = None,
    evaluation_contract: FalsificationEvaluationContractV1,
    trace_id: str,
    config_hash: str,
    code_hash: str,
    created_at: datetime,
) -> CandidateEvaluationResult:
    """Execute every PIT request twice and let the trusted host calculate PnL."""

    execution = execute_candidate_dataset_twice(
        dataset=dataset,
        executor=executor,
        replay_executor=replay_executor,
        config_hash=config_hash,
        code_hash=code_hash,
        created_at=created_at,
    )
    observations = _build_observations(
        dataset,
        execution.responses,
        evaluation_contract,
    )
    trace = build_candidate_evaluation_trace(
        trace_id=trace_id,
        challenger_id=dataset.challenger_id,
        candidate_artifact_hash=dataset.candidate_artifact_hash,
        data_manifest_hash=dataset.source_data_manifest_hash,
        evaluation_contract=evaluation_contract,
        eligible_instrument_count=dataset.eligible_instrument_count,
        eligible_non_survivor_count=dataset.eligible_non_survivor_count,
        observations=observations,
        created_at=created_at,
    )
    return CandidateEvaluationResult(trace=trace, replay=execution.replay)


def execute_candidate_dataset_twice(
    *,
    dataset: CandidateEvaluationDatasetV1,
    executor: CandidateExecutor,
    replay_executor: CandidateExecutor | None = None,
    config_hash: str,
    code_hash: str,
    created_at: datetime,
) -> CandidateExecutionReplayResult:
    """Return deterministic candidate outputs without exposing future outcomes."""

    first = _execute_dataset(dataset, executor)
    second = _execute_dataset(dataset, replay_executor or executor)
    first_hash = _execution_hash(dataset, first)
    second_hash = _execution_hash(dataset, second)
    replay = DeterministicReplayArtifactV1(
        challenger_id=dataset.challenger_id,
        candidate_artifact_hash=dataset.candidate_artifact_hash,
        config_hash=config_hash,
        code_hash=code_hash,
        data_manifest_hash=dataset.source_data_manifest_hash,
        first_replay_hash=first_hash,
        second_replay_hash=second_hash,
        created_at=created_at,
    )
    if not replay.deterministic_match:
        raise CandidateEvaluationError("candidate outputs are not deterministic")
    return CandidateExecutionReplayResult(responses=first, replay=replay)


def _execute_dataset(
    dataset: CandidateEvaluationDatasetV1,
    executor: CandidateExecutor,
) -> tuple[CandidateDecisionResponseV1, ...]:
    responses: list[CandidateDecisionResponseV1] = []
    for scenario in dataset.scenarios:
        try:
            response = CandidateDecisionResponseV1.model_validate(
                executor.execute(scenario.request)
            )
            response.assert_bound_to(scenario.request)
        except Exception as exc:
            raise CandidateEvaluationError(
                f"candidate execution rejected for scenario {scenario.scenario_id}: "
                f"{type(exc).__name__}"
            ) from None
        responses.append(response)
    return tuple(responses)


def _execution_hash(
    dataset: CandidateEvaluationDatasetV1,
    responses: tuple[CandidateDecisionResponseV1, ...],
) -> str:
    return canonical_hash(
        {
            "schema_version": "candidate_execution_replay_v1",
            "dataset_hash": dataset.dataset_hash,
            "request_hashes": [
                item.request.request_hash for item in dataset.scenarios
            ],
            "response_hashes": [item.output_hash for item in responses],
        }
    )


def _build_observations(
    dataset: CandidateEvaluationDatasetV1,
    responses: tuple[CandidateDecisionResponseV1, ...],
    contract: FalsificationEvaluationContractV1,
) -> tuple[CandidateEvaluationObservationV1, ...]:
    rows: list[CandidateEvaluationObservationV1] = []
    for scenario, response in zip(dataset.scenarios, responses, strict=True):
        targets = {item.symbol: item for item in response.targets}
        inputs = {item.symbol: item for item in scenario.request.instruments}
        for outcome in scenario.outcomes:
            candidate_input = inputs[outcome.symbol]
            target = targets[outcome.symbol]
            features = candidate_input.features
            turnover_weight = abs(target.target_weight - candidate_input.current_weight)
            modeled_cost_bps = (
                outcome.commission_bps
                + outcome.spread_bps / 2
                + outcome.delay_bps
            )
            modeled_cost = (
                turnover_weight
                * modeled_cost_bps
                / contract.basis_points_per_unit_return
            )
            latest_available = max(
                candidate_input.membership_available_at,
                *(item.available_at for item in features),
            )
            latest_source_event = max(item.source_event_time for item in features)
            latest_revision_available = max(
                item.revision_available_at for item in features
            )
            source_hashes = tuple(sorted({item.source_hash for item in features}))
            variant = scenario.request.variant
            rows.append(
                CandidateEvaluationObservationV1(
                    observation_id=_observation_id(
                        scenario.scenario_id,
                        outcome.symbol,
                    ),
                    decision_time=scenario.request.decision_time,
                    signal_data_cutoff=scenario.request.signal_data_cutoff,
                    available_at=latest_available,
                    source_event_time=latest_source_event,
                    outcome_available_at=outcome.outcome_available_at,
                    constituent_membership_available_at=(
                        candidate_input.membership_available_at
                    ),
                    constituent_valid_from=candidate_input.membership_valid_from,
                    constituent_valid_until=candidate_input.membership_valid_until,
                    revision_available_at=latest_revision_available,
                    source_revision=max(item.source_revision for item in features),
                    revision_was_known_at_cutoff=all(
                        item.revision_was_known_at_cutoff for item in features
                    ),
                    instrument_id=outcome.symbol,
                    instrument_is_non_survivor=(
                        candidate_input.instrument_is_non_survivor
                    ),
                    trade_id=outcome.trade_id,
                    candidate_score=target.score,
                    candidate_target=target.target_weight,
                    candidate_return=target.target_weight * outcome.forward_return,
                    baseline_return=(
                        outcome.baseline_target_weight * outcome.forward_return
                    ),
                    modeled_cost=modeled_cost,
                    modeled_spread_bps=outcome.spread_bps,
                    modeled_delay_bps=outcome.delay_bps,
                    adv_usd=outcome.adv_usd,
                    capacity_used_usd=turnover_weight * scenario.evaluation_nav_usd,
                    market_return=outcome.market_return,
                    sector_return=outcome.sector_return,
                    known_factor_returns=outcome.known_factor_returns,
                    regime=outcome.regime,
                    parameter_neighborhood_id=variant.parameter_neighborhood_id,
                    data_ablation_id=variant.data_ablation_id,
                    date_shift_id=variant.date_shift_id,
                    inversion_id=variant.inversion_id,
                    shuffle_id=variant.shuffle_id,
                    source_hashes=source_hashes,
                )
            )
    return tuple(sorted(rows, key=lambda item: item.unique_key))


def _observation_id(scenario_id: str, symbol: str) -> str:
    return "candidate_observation_" + canonical_hash(
        {"scenario_id": scenario_id, "symbol": symbol}
    )[:24]
