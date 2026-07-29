from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.research.candidate_abi import (
    CandidateDecisionRequestV1,
    CandidateDecisionResponseV1,
    CandidateExecutor,
)
from trading.research.contracts import (
    HASH_PATTERN,
    IDENTIFIER_PATTERN,
    VERSION_PATTERN,
)
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


class CandidateEvaluationScenarioSourceBindingV2(DomainModel):
    """Immutable provenance for one scenario in a multi-cutoff dataset."""

    schema_version: Literal[
        "candidate_evaluation_scenario_source_binding_v2"
    ] = "candidate_evaluation_scenario_source_binding_v2"
    scenario_id: str = Field(pattern=IDENTIFIER_PATTERN)
    scenario_hash: str = Field(pattern=HASH_PATTERN)
    request_hash: str = Field(pattern=HASH_PATTERN)
    request_source_manifest_hash: str = Field(pattern=HASH_PATTERN)
    base_scenario_id: str = Field(pattern=IDENTIFIER_PATTERN)
    base_request_hash: str = Field(pattern=HASH_PATTERN)
    base_source_manifest_hash: str = Field(pattern=HASH_PATTERN)
    calendar_path_hash: str = Field(pattern=HASH_PATTERN)
    outcome_source_hash: str = Field(pattern=HASH_PATTERN)
    outcome_available_at: datetime
    transformation_hash: str = Field(pattern=HASH_PATTERN)
    binding_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("outcome_available_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"binding_hash"})
        if canonical_hash(payload) != self.binding_hash:
            raise ValueError("candidate evaluation source binding hash mismatch")
        return self


class CandidateEvaluationCohortEntryV2(DomainModel):
    """One selected forward request and its hidden realized outcome."""

    schema_version: Literal[
        "candidate_evaluation_cohort_entry_v2"
    ] = "candidate_evaluation_cohort_entry_v2"
    prospective_request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    request_hash: str = Field(pattern=HASH_PATTERN)
    decision_time: datetime
    signal_data_cutoff: datetime
    outcome_source_hash: str = Field(pattern=HASH_PATTERN)
    outcome_available_at: datetime
    entry_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "decision_time",
        "signal_data_cutoff",
        "outcome_available_at",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_entry(self) -> Self:
        if not (
            self.signal_data_cutoff <= self.decision_time
            < self.outcome_available_at
        ):
            raise ValueError(
                "candidate evaluation cohort timestamps are invalid"
            )
        payload = self.model_dump(mode="python", exclude={"entry_hash"})
        if canonical_hash(payload) != self.entry_hash:
            raise ValueError("candidate evaluation cohort entry hash mismatch")
        return self


class CandidateEvaluationCohortManifestV2(DomainModel):
    """Frozen success/failure cohort selected before evaluation begins."""

    schema_version: Literal[
        "candidate_evaluation_cohort_manifest_v2"
    ] = "candidate_evaluation_cohort_manifest_v2"
    selection_policy: str = Field(pattern=IDENTIFIER_PATTERN)
    required_successful_sessions: int = Field(gt=0)
    entries: tuple[CandidateEvaluationCohortEntryV2, ...] = Field(
        min_length=1
    )
    terminal_failure_hashes: tuple[str, ...] = ()
    terminal_request_count: int = Field(gt=0)
    selection_data_cutoff: datetime
    manifest_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("selection_data_cutoff", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("entries", mode="after")
    @classmethod
    def validate_entries(
        cls,
        value: tuple[CandidateEvaluationCohortEntryV2, ...],
    ) -> tuple[CandidateEvaluationCohortEntryV2, ...]:
        keys = tuple(
            (item.decision_time, item.prospective_request_id)
            for item in value
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError(
                "candidate evaluation cohort entries must be unique and sorted"
            )
        return value

    @field_validator("terminal_failure_hashes", mode="after")
    @classmethod
    def validate_failure_hashes(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError(
                "candidate evaluation failure hashes must be unique and sorted"
            )
        if any(
            re.fullmatch(HASH_PATTERN, item) is None
            for item in value
        ):
            raise ValueError("candidate evaluation failure hash is invalid")
        return value

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if (
            len(self.entries) != self.required_successful_sessions
            or self.terminal_request_count
            != len(self.entries) + len(self.terminal_failure_hashes)
            or self.selection_data_cutoff
            < max(item.outcome_available_at for item in self.entries)
        ):
            raise ValueError(
                "candidate evaluation cohort completeness is invalid"
            )
        payload = self.model_dump(mode="python", exclude={"manifest_hash"})
        if canonical_hash(payload) != self.manifest_hash:
            raise ValueError(
                "candidate evaluation cohort manifest hash mismatch"
            )
        return self


class CandidateEvaluationSourceManifestV2(DomainModel):
    """Aggregate manifest that preserves every scenario's distinct PIT cutoff."""

    schema_version: Literal[
        "candidate_evaluation_source_manifest_v2"
    ] = "candidate_evaluation_source_manifest_v2"
    producer_version: str = Field(pattern=VERSION_PATTERN)
    config_manifest_hash: str = Field(pattern=HASH_PATTERN)
    cohort_manifest: CandidateEvaluationCohortManifestV2
    bindings: tuple[CandidateEvaluationScenarioSourceBindingV2, ...] = Field(
        min_length=1
    )
    manifest_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("bindings", mode="after")
    @classmethod
    def validate_bindings(
        cls,
        value: tuple[CandidateEvaluationScenarioSourceBindingV2, ...],
    ) -> tuple[CandidateEvaluationScenarioSourceBindingV2, ...]:
        keys = tuple(item.scenario_id for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError(
                "candidate evaluation source bindings must be unique and sorted"
            )
        return value

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"manifest_hash"})
        if canonical_hash(payload) != self.manifest_hash:
            raise ValueError(
                "candidate evaluation aggregate source manifest hash mismatch"
            )
        return self


class CandidateEvaluationDatasetV2(DomainModel):
    """Multi-cutoff evaluation dataset without changing the V1 contract."""

    schema_version: Literal[
        "candidate_evaluation_dataset_v2"
    ] = "candidate_evaluation_dataset_v2"
    dataset_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    source_manifest: CandidateEvaluationSourceManifestV2
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
            raise ValueError("candidate V2 scenarios must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if self.eligible_non_survivor_count > self.eligible_instrument_count:
            raise ValueError("eligible non-survivor count exceeds universe")
        bindings = {
            item.scenario_id: item for item in self.source_manifest.bindings
        }
        if set(bindings) != {item.scenario_id for item in self.scenarios}:
            raise ValueError(
                "candidate V2 source bindings differ from scenarios"
            )
        base_key = ("BASE", "BASE", "BASE", "BASE", "BASE")
        base_scenarios = {
            item.scenario_id: item
            for item in self.scenarios
            if item.request.variant.key == base_key
        }
        cohort_by_request_hash = {
            item.request_hash: item
            for item in self.source_manifest.cohort_manifest.entries
        }
        if (
            len(base_scenarios)
            != self.source_manifest.cohort_manifest.required_successful_sessions
            or len(cohort_by_request_hash) != len(base_scenarios)
        ):
            raise ValueError(
                "candidate V2 base scenarios differ from selected cohort"
            )
        for scenario in self.scenarios:
            request = scenario.request
            binding = bindings[scenario.scenario_id]
            base = base_scenarios.get(binding.base_scenario_id)
            cohort = (
                None
                if base is None
                else cohort_by_request_hash.get(base.request.request_hash)
            )
            if (
                request.challenger_id != self.challenger_id
                or request.candidate_artifact_hash
                != self.candidate_artifact_hash
                or binding.scenario_hash != scenario.scenario_hash
                or binding.request_hash != request.request_hash
                or binding.request_source_manifest_hash
                != request.source_data_manifest_hash
                or base is None
                or cohort is None
                or binding.base_request_hash != base.request.request_hash
                or binding.base_source_manifest_hash
                != base.request.source_data_manifest_hash
                or binding.calendar_path_hash
                != bindings[base.scenario_id].calendar_path_hash
                or binding.outcome_source_hash
                != cohort.outcome_source_hash
                or (
                    scenario.request.variant.key == base_key
                    and binding.base_scenario_id != scenario.scenario_id
                )
                or binding.outcome_available_at
                != max(
                    item.outcome_available_at
                    for item in scenario.outcomes
                )
            ):
                raise ValueError("candidate V2 scenario binding mismatch")
        payload = self.model_dump(mode="python", exclude={"dataset_hash"})
        if canonical_hash(payload) != self.dataset_hash:
            raise ValueError("candidate evaluation V2 dataset hash mismatch")
        return self


CandidateEvaluationDataset = (
    CandidateEvaluationDatasetV1 | CandidateEvaluationDatasetV2
)


@dataclass(frozen=True, slots=True)
class CandidateEvaluationResult:
    trace: CandidateEvaluationTraceV1
    replay: DeterministicReplayArtifactV1


@dataclass(frozen=True, slots=True)
class CandidateExecutionReplayResult:
    responses: tuple[CandidateDecisionResponseV1, ...]
    replay: DeterministicReplayArtifactV1


class CandidateEvaluationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        replay: DeterministicReplayArtifactV1 | None = None,
    ) -> None:
        self.replay = replay
        super().__init__(message)


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


def build_candidate_evaluation_source_binding_v2(
    *,
    scenario: CandidateEvaluationScenarioV1,
    base_scenario_id: str,
    base_request_hash: str,
    base_source_manifest_hash: str,
    calendar_path_hash: str,
    outcome_source_hash: str,
    transformation_hash: str,
) -> CandidateEvaluationScenarioSourceBindingV2:
    payload = {
        "schema_version": (
            "candidate_evaluation_scenario_source_binding_v2"
        ),
        "scenario_id": scenario.scenario_id,
        "scenario_hash": scenario.scenario_hash,
        "request_hash": scenario.request.request_hash,
        "request_source_manifest_hash": (
            scenario.request.source_data_manifest_hash
        ),
        "base_scenario_id": base_scenario_id,
        "base_request_hash": base_request_hash,
        "base_source_manifest_hash": base_source_manifest_hash,
        "calendar_path_hash": calendar_path_hash,
        "outcome_source_hash": outcome_source_hash,
        "outcome_available_at": max(
            item.outcome_available_at for item in scenario.outcomes
        ),
        "transformation_hash": transformation_hash,
    }
    return CandidateEvaluationScenarioSourceBindingV2.model_validate(
        {**payload, "binding_hash": canonical_hash(payload)}
    )


def build_candidate_evaluation_cohort_entry_v2(
    *,
    prospective_request_id: str,
    request_hash: str,
    decision_time: datetime,
    signal_data_cutoff: datetime,
    outcome_source_hash: str,
    outcome_available_at: datetime,
) -> CandidateEvaluationCohortEntryV2:
    payload = {
        "schema_version": "candidate_evaluation_cohort_entry_v2",
        "prospective_request_id": prospective_request_id,
        "request_hash": request_hash,
        "decision_time": decision_time,
        "signal_data_cutoff": signal_data_cutoff,
        "outcome_source_hash": outcome_source_hash,
        "outcome_available_at": outcome_available_at,
    }
    return CandidateEvaluationCohortEntryV2.model_validate(
        {**payload, "entry_hash": canonical_hash(payload)}
    )


def build_candidate_evaluation_cohort_manifest_v2(
    *,
    selection_policy: str,
    required_successful_sessions: int,
    entries: tuple[CandidateEvaluationCohortEntryV2, ...],
    terminal_failure_hashes: tuple[str, ...],
    terminal_request_count: int,
    selection_data_cutoff: datetime,
) -> CandidateEvaluationCohortManifestV2:
    ordered_entries = tuple(
        sorted(
            entries,
            key=lambda item: (
                item.decision_time,
                item.prospective_request_id,
            ),
        )
    )
    payload = {
        "schema_version": "candidate_evaluation_cohort_manifest_v2",
        "selection_policy": selection_policy,
        "required_successful_sessions": required_successful_sessions,
        "entries": ordered_entries,
        "terminal_failure_hashes": tuple(
            sorted(terminal_failure_hashes)
        ),
        "terminal_request_count": terminal_request_count,
        "selection_data_cutoff": selection_data_cutoff,
    }
    return CandidateEvaluationCohortManifestV2.model_validate(
        {**payload, "manifest_hash": canonical_hash(payload)}
    )


def build_candidate_evaluation_source_manifest_v2(
    *,
    producer_version: str,
    config_manifest_hash: str,
    cohort_manifest: CandidateEvaluationCohortManifestV2,
    bindings: tuple[CandidateEvaluationScenarioSourceBindingV2, ...],
) -> CandidateEvaluationSourceManifestV2:
    ordered = tuple(sorted(bindings, key=lambda item: item.scenario_id))
    payload = {
        "schema_version": "candidate_evaluation_source_manifest_v2",
        "producer_version": producer_version,
        "config_manifest_hash": config_manifest_hash,
        "cohort_manifest": cohort_manifest,
        "bindings": ordered,
    }
    return CandidateEvaluationSourceManifestV2.model_validate(
        {**payload, "manifest_hash": canonical_hash(payload)}
    )


def build_candidate_evaluation_dataset_v2(
    *,
    dataset_id: str,
    challenger_id: str,
    candidate_artifact_hash: str,
    source_manifest: CandidateEvaluationSourceManifestV2,
    eligible_instrument_count: int,
    eligible_non_survivor_count: int,
    scenarios: tuple[CandidateEvaluationScenarioV1, ...],
) -> CandidateEvaluationDatasetV2:
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
        "schema_version": "candidate_evaluation_dataset_v2",
        "dataset_id": dataset_id,
        "challenger_id": challenger_id,
        "candidate_artifact_hash": candidate_artifact_hash,
        "source_manifest": source_manifest,
        "eligible_instrument_count": eligible_instrument_count,
        "eligible_non_survivor_count": eligible_non_survivor_count,
        "scenarios": ordered,
    }
    return CandidateEvaluationDatasetV2.model_validate(
        {**payload, "dataset_hash": canonical_hash(payload)}
    )


def evaluate_candidate_twice(
    *,
    dataset: CandidateEvaluationDataset,
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
        data_manifest_hash=_dataset_source_manifest_hash(dataset),
        evaluation_contract=evaluation_contract,
        eligible_instrument_count=dataset.eligible_instrument_count,
        eligible_non_survivor_count=dataset.eligible_non_survivor_count,
        observations=observations,
        created_at=created_at,
    )
    return CandidateEvaluationResult(trace=trace, replay=execution.replay)


def execute_candidate_dataset_twice(
    *,
    dataset: CandidateEvaluationDataset,
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
        data_manifest_hash=_dataset_source_manifest_hash(dataset),
        first_replay_hash=first_hash,
        second_replay_hash=second_hash,
        created_at=created_at,
    )
    if not replay.deterministic_match:
        raise CandidateEvaluationError(
            "candidate outputs are not deterministic",
            replay=replay,
        )
    return CandidateExecutionReplayResult(responses=first, replay=replay)


def _execute_dataset(
    dataset: CandidateEvaluationDataset,
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
    dataset: CandidateEvaluationDataset,
    responses: tuple[CandidateDecisionResponseV1, ...],
) -> str:
    return canonical_hash(
        {
            "schema_version": (
                "candidate_execution_replay_v1"
                if isinstance(dataset, CandidateEvaluationDatasetV1)
                else "candidate_execution_replay_v2"
            ),
            "dataset_hash": dataset.dataset_hash,
            "request_hashes": [
                item.request.request_hash for item in dataset.scenarios
            ],
            "response_hashes": [item.output_hash for item in responses],
        }
    )


def _build_observations(
    dataset: CandidateEvaluationDataset,
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


def _dataset_source_manifest_hash(
    dataset: CandidateEvaluationDataset,
) -> str:
    if isinstance(dataset, CandidateEvaluationDatasetV1):
        return dataset.source_data_manifest_hash
    return dataset.source_manifest.manifest_hash


def _observation_id(scenario_id: str, symbol: str) -> str:
    return "candidate_observation_" + canonical_hash(
        {"scenario_id": scenario_id, "symbol": symbol}
    )[:24]
