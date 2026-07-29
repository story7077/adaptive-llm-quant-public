from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

import yaml
from pydantic import Field, JsonValue, field_validator, model_validator

from trading.data.q1_pit import AlignedDailyInputs
from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, stable_id
from trading.research.candidate_abi import (
    CandidateDecisionRequestV1,
    CandidateDecisionResponseV1,
    CandidateEvaluationVariantV1,
    CandidateExecutor,
    CandidateFeatureValueV1,
    CandidateInstrumentInputV1,
    build_candidate_decision_request,
)
from trading.research.candidate_evaluation import (
    CandidateEvaluationDatasetV2,
    CandidateEvaluationScenarioSourceBindingV2,
    CandidateEvaluationScenarioV1,
    CandidateOutcomeV1,
    build_candidate_evaluation_cohort_entry_v2,
    build_candidate_evaluation_cohort_manifest_v2,
    build_candidate_evaluation_dataset_v2,
    build_candidate_evaluation_scenario,
    build_candidate_evaluation_source_binding_v2,
    build_candidate_evaluation_source_manifest_v2,
)
from trading.research.contracts import (
    IDENTIFIER_PATTERN,
    SYMBOL_PATTERN,
    VERSION_PATTERN,
)
from trading.research.evaluation_contracts import (
    BASE_VARIANT_ID,
    KnownFactorReturnV1,
)
from trading.research.prospective import (
    PROSPECTIVE_CONFIG_FILE,
    ProspectiveExecutionEvidenceV1,
    ProspectiveExecutionStatus,
    ProspectiveRequestEvidenceV1,
    build_candidate_price_features,
    load_prospective_candidate_config,
)
from trading.research.prospective_outcomes import (
    PROSPECTIVE_OUTCOME_CONFIG_FILE,
    ProspectiveOutcomeEvidenceV1,
    ProspectiveOutcomeFailureV1,
    load_prospective_outcome_config,
)

PROSPECTIVE_EVALUATION_CONFIG_FILE = (
    "research/candidate-prospective-evaluation.yaml"
)
_PRICE_FEATURE_PREFIXES = (
    "total_return_",
    "moving_average_gap_",
    "realized_volatility_",
    "downside_beta_",
    "downside_observation_count_",
)


class ProspectiveEvaluationError(RuntimeError):
    """Stable fail-closed error for trusted forward dataset assembly."""


class ProspectiveEvaluationSourceSelectionV1(DomainModel):
    policy: Literal["FIRST_N_SUCCESSFUL_FORWARD_SESSIONS"]
    required_common_sessions: int = Field(ge=126)
    minimum_request_coverage_ratio: float = Field(gt=0, le=1)
    minimum_variant_session_coverage_ratio: float = Field(gt=0, le=1)
    numeric_tolerance: float = Field(gt=0)


class ProspectiveEvaluationUniverseV1(DomainModel):
    eligible_instrument_count: int = Field(gt=0)
    eligible_non_survivor_count: int = Field(ge=0)
    symbols: tuple[str, ...] = Field(min_length=1)

    @field_validator("symbols", mode="after")
    @classmethod
    def validate_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError(
                "prospective evaluation symbols must be unique and sorted"
            )
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if (
            self.eligible_instrument_count != len(self.symbols)
            or self.eligible_non_survivor_count
            > self.eligible_instrument_count
        ):
            raise ValueError(
                "prospective evaluation universe counts are invalid"
            )
        return self


class ProspectiveEvaluationStateV1(DomainModel):
    base_state_source: Literal["STORED_PROSPECTIVE_REQUEST"]
    variant_initial_state_source: Literal[
        "FIRST_BASE_REQUEST_CURRENT_WEIGHTS"
    ]
    variant_subsequent_state_source: Literal["PRIOR_VARIANT_TARGETS"]
    review_clock_source: Literal["VERSIONED_MARKET_CALENDAR"]


class ProspectiveEvaluationFeatureCalculationV1(DomainModel):
    annualization_sessions: int = Field(gt=1)
    realized_volatility_ddof: int = Field(ge=0)
    minimum_variance: float = Field(gt=0)
    source_revision: int = Field(ge=0)


class ProspectiveEvaluationTransformConstantsV1(DomainModel):
    removed_downside_beta: float
    static_excess_return: float
    static_moving_average_gap: float
    static_realized_volatility: float = Field(gt=0)
    static_downside_beta: float
    static_minimum_downside_observations: int = Field(gt=1)

    @field_validator(
        "removed_downside_beta",
        "static_excess_return",
        "static_moving_average_gap",
        "static_realized_volatility",
        "static_downside_beta",
        mode="after",
    )
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError(
                "prospective evaluation transform must be finite"
            )
        return value


class ProspectiveParameterVariantV1(DomainModel):
    variant_id: str = Field(pattern=IDENTIFIER_PATTERN)
    required_history_sessions: int = Field(gt=1)
    parameter_overrides: dict[str, int | float] = Field(min_length=1)

    @field_validator("parameter_overrides", mode="after")
    @classmethod
    def validate_overrides(
        cls,
        value: dict[str, int | float],
    ) -> dict[str, int | float]:
        if list(value) != sorted(value):
            raise ValueError("parameter overrides must be sorted")
        for item in value.values():
            if isinstance(item, bool) or not math.isfinite(float(item)):
                raise ValueError("parameter override must be finite")
        return value


class ProspectiveDataAblationV1(DomainModel):
    variant_id: str = Field(pattern=IDENTIFIER_PATTERN)
    symbol: str = Field(pattern=SYMBOL_PATTERN)


class ProspectiveDateShiftV1(DomainModel):
    variant_id: str = Field(pattern=IDENTIFIER_PATTERN)
    review_clock_offset_sessions: int = Field(gt=0)


class ProspectivePlaceboV1(DomainModel):
    variant_id: str = Field(pattern=IDENTIFIER_PATTERN)
    transform: Literal[
        "INVERT_EXCESS_TREND",
        "REMOVE_DOWNSIDE_BETA_GATE",
        "SGOV_ONLY",
        "STATIC_GLD_TLT",
    ]


class ProspectiveSymbolShuffleV1(DomainModel):
    variant_id: str = Field(pattern=IDENTIFIER_PATTERN)
    left_symbol: str = Field(pattern=SYMBOL_PATTERN)
    right_symbol: str = Field(pattern=SYMBOL_PATTERN)

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        if self.left_symbol >= self.right_symbol:
            raise ValueError("symbol shuffle pair must be canonically ordered")
        return self


class ProspectiveEvaluationSafetyV1(DomainModel):
    real_order_routing: Literal[False] = False
    broker_access_permitted: Literal[False] = False
    credential_access_permitted: Literal[False] = False
    automatic_promotion_enabled: Literal[False] = False
    challenger_lifecycle_advance_before_falsification: Literal[False] = False
    oos_access_permitted: Literal[False] = False
    shadow_activation_enabled: Literal[False] = False


class ProspectiveEvaluationConfigV1(DomainModel):
    schema_version: Literal["candidate_prospective_evaluation_config_v1"]
    producer_version: str = Field(pattern=VERSION_PATTERN)
    deterministic_seed: int = Field(ge=0)
    strategy_config_path: str
    prospective_config_path: str
    outcome_config_path: str
    source_selection: ProspectiveEvaluationSourceSelectionV1
    universe: ProspectiveEvaluationUniverseV1
    state: ProspectiveEvaluationStateV1
    feature_calculation: ProspectiveEvaluationFeatureCalculationV1
    transform_constants: ProspectiveEvaluationTransformConstantsV1
    parameter_neighborhoods: tuple[ProspectiveParameterVariantV1, ...] = (
        Field(min_length=1)
    )
    data_ablations: tuple[ProspectiveDataAblationV1, ...] = Field(
        min_length=1
    )
    date_shifts: tuple[ProspectiveDateShiftV1, ...] = Field(min_length=1)
    placebos: tuple[ProspectivePlaceboV1, ...] = Field(min_length=1)
    symbol_shuffles: tuple[ProspectiveSymbolShuffleV1, ...] = Field(
        min_length=1
    )
    safety: ProspectiveEvaluationSafetyV1

    @field_validator(
        "strategy_config_path",
        "prospective_config_path",
        "outcome_config_path",
        mode="after",
    )
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                "prospective evaluation config path escaped config"
            )
        return value

    @model_validator(mode="after")
    def validate_variants(self) -> Self:
        collections = (
            self.parameter_neighborhoods,
            self.data_ablations,
            self.date_shifts,
            self.placebos,
            self.symbol_shuffles,
        )
        all_ids = tuple(
            item.variant_id
            for collection in collections
            for item in collection
        )
        if len(all_ids) != len(set(all_ids)):
            raise ValueError(
                "prospective evaluation variant IDs must be unique"
            )
        for collection in collections:
            ids = tuple(item.variant_id for item in collection)
            if ids != tuple(sorted(ids)):
                raise ValueError(
                    "prospective evaluation variants must be sorted"
                )
        if any(
            item.symbol not in self.universe.symbols
            for item in self.data_ablations
        ):
            raise ValueError("data ablation symbol is outside the universe")
        if any(
            item.left_symbol not in self.universe.symbols
            or item.right_symbol not in self.universe.symbols
            for item in self.symbol_shuffles
        ):
            raise ValueError("symbol shuffle is outside the universe")
        return self


@dataclass(frozen=True, slots=True)
class ProspectiveEvaluationConfigBundle:
    config: ProspectiveEvaluationConfigV1
    strategy_parameters: dict[str, JsonValue]
    strategy_document_hash: str
    prospective_manifest_hash: str
    outcome_manifest_hash: str
    manifest_hash: str
    path: Path


@dataclass(frozen=True, slots=True)
class ProspectiveEvaluationRecord:
    request: ProspectiveRequestEvidenceV1
    execution: ProspectiveExecutionEvidenceV1
    outcome: ProspectiveOutcomeEvidenceV1
    market_inputs: AlignedDailyInputs
    decision_session_ordinal: int
    calendar_path_hash: str


@dataclass(frozen=True, slots=True)
class ProspectiveEvaluationBuildResult:
    dataset: CandidateEvaluationDatasetV2
    selected_request_count: int
    terminal_failure_count: int
    request_coverage_ratio: float
    base_scenario_count: int
    variant_scenario_count: int
    variant_coverage: dict[str, float]
    logical_created_at: datetime


@dataclass(frozen=True, slots=True)
class _VariantPlan:
    variant: CandidateEvaluationVariantV1
    required_history_sessions: int
    parameter_overrides: dict[str, int | float]
    ablation_symbol: str | None = None
    review_clock_offset_sessions: int = 0
    placebo_transform: str | None = None
    shuffle_pair: tuple[str, str] | None = None

    @property
    def variant_id(self) -> str:
        return next(
            item
            for item in self.variant.key
            if item != BASE_VARIANT_ID
        )


def load_prospective_evaluation_config(
    config_dir: Path,
) -> ProspectiveEvaluationConfigBundle:
    root = config_dir.resolve(strict=True)
    path = (root / PROSPECTIVE_EVALUATION_CONFIG_FILE).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("prospective evaluation config escaped config")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(
            "prospective evaluation config must be a YAML object"
        )
    config = ProspectiveEvaluationConfigV1.model_validate(loaded)
    prospective = load_prospective_candidate_config(root)
    outcomes = load_prospective_outcome_config(root)
    if (
        config.strategy_config_path
        != prospective.config.strategy_config_path
        or config.prospective_config_path != PROSPECTIVE_CONFIG_FILE
        or config.outcome_config_path != PROSPECTIVE_OUTCOME_CONFIG_FILE
        or config.universe.symbols
        != prospective.config.reference_universe
        or config.source_selection.required_common_sessions
        != outcomes.config.readiness.minimum_common_sessions
    ):
        raise ValueError(
            "prospective evaluation dependency binding mismatch"
        )
    _validate_strategy_variants(
        config=config,
        strategy_document=prospective.strategy_document,
    )
    maximum_history = max(
        item.required_history_sessions
        for item in config.parameter_neighborhoods
    )
    if (
        prospective.config.market_data.required_completed_sessions
        < maximum_history
    ):
        raise ValueError(
            "prospective source history cannot satisfy evaluation variants"
        )
    manifest_payload = {
        PROSPECTIVE_EVALUATION_CONFIG_FILE: config,
        PROSPECTIVE_CONFIG_FILE: prospective.manifest_hash,
        PROSPECTIVE_OUTCOME_CONFIG_FILE: outcomes.manifest_hash,
        config.strategy_config_path: prospective.strategy_document_hash,
    }
    return ProspectiveEvaluationConfigBundle(
        config=config,
        strategy_parameters=dict(prospective.strategy_parameters),
        strategy_document_hash=prospective.strategy_document_hash,
        prospective_manifest_hash=prospective.manifest_hash,
        outcome_manifest_hash=outcomes.manifest_hash,
        manifest_hash=canonical_hash(manifest_payload),
        path=path,
    )


def build_prospective_evaluation_dataset(
    *,
    config_bundle: ProspectiveEvaluationConfigBundle,
    records: tuple[ProspectiveEvaluationRecord, ...],
    terminal_failures: tuple[ProspectiveOutcomeFailureV1, ...],
    state_executor: CandidateExecutor,
) -> ProspectiveEvaluationBuildResult:
    """Build the first immutable multi-cutoff forward dataset."""

    config = config_bundle.config
    required = config.source_selection.required_common_sessions
    ordered = tuple(
        sorted(
            records,
            key=lambda item: (
                item.request.request.decision_time,
                item.request.prospective_request_id,
            ),
        )
    )
    if len(ordered) < required:
        raise ProspectiveEvaluationError(
            "PROSPECTIVE_EVALUATION_NOT_READY"
        )
    selected = ordered[:required]
    failure_hashes = tuple(
        sorted(item.failure_hash for item in terminal_failures)
    )
    if len(failure_hashes) != len(set(failure_hashes)):
        raise ProspectiveEvaluationError(
            "PROSPECTIVE_EVALUATION_FAILURES_NOT_UNIQUE"
        )
    terminal_failure_count = len(terminal_failures)
    coverage = required / (required + terminal_failure_count)
    if (
        coverage
        + config.source_selection.numeric_tolerance
        < config.source_selection.minimum_request_coverage_ratio
    ):
        raise ProspectiveEvaluationError(
            "PROSPECTIVE_EVALUATION_REQUEST_COVERAGE_INSUFFICIENT"
        )
    challenger_id, candidate_artifact_hash = _validate_records(
        config_bundle=config_bundle,
        records=selected,
    )
    if any(
        item.challenger_id != challenger_id
        or item.candidate_artifact_hash != candidate_artifact_hash
        or item.config_manifest_hash != config_bundle.outcome_manifest_hash
        for item in terminal_failures
    ):
        raise ProspectiveEvaluationError(
            "PROSPECTIVE_EVALUATION_FAILURE_BINDING_INVALID"
        )
    scenarios: list[CandidateEvaluationScenarioV1] = []
    bindings: list[CandidateEvaluationScenarioSourceBindingV2] = []
    for record in selected:
        scenario, binding = _base_scenario(
            record=record,
            config_bundle=config_bundle,
        )
        scenarios.append(scenario)
        bindings.append(binding)

    variant_coverage: dict[str, float] = {}
    for plan in _variant_plans(config):
        built = _variant_scenarios(
            plan=plan,
            records=selected,
            config_bundle=config_bundle,
            state_executor=state_executor,
        )
        variant_coverage[plan.variant_id] = len(built) / required
        if (
            variant_coverage[plan.variant_id]
            + config.source_selection.numeric_tolerance
            < config.source_selection.minimum_variant_session_coverage_ratio
        ):
            raise ProspectiveEvaluationError(
                "PROSPECTIVE_EVALUATION_VARIANT_COVERAGE_INSUFFICIENT:"
                + plan.variant_id
            )
        for scenario, binding in built:
            scenarios.append(scenario)
            bindings.append(binding)

    selection_data_cutoff = max(
        (
            *(item.outcome.outcome_data_cutoff for item in selected),
            *(item.outcome_data_cutoff for item in terminal_failures),
        )
    )
    cohort_manifest = build_candidate_evaluation_cohort_manifest_v2(
        selection_policy=config.source_selection.policy,
        required_successful_sessions=required,
        entries=tuple(
            build_candidate_evaluation_cohort_entry_v2(
                prospective_request_id=(
                    item.request.prospective_request_id
                ),
                request_hash=item.request.request.request_hash,
                decision_time=item.request.request.decision_time,
                signal_data_cutoff=(
                    item.request.request.signal_data_cutoff
                ),
                outcome_source_hash=item.outcome.outcome_hash,
                outcome_available_at=item.outcome.outcome_available_at,
            )
            for item in selected
        ),
        terminal_failure_hashes=failure_hashes,
        terminal_request_count=required + terminal_failure_count,
        selection_data_cutoff=selection_data_cutoff,
    )
    source_manifest = build_candidate_evaluation_source_manifest_v2(
        producer_version=config.producer_version,
        config_manifest_hash=config_bundle.manifest_hash,
        cohort_manifest=cohort_manifest,
        bindings=tuple(bindings),
    )
    dataset_id = stable_id(
        "candidate-evaluation-dataset-v2",
        challenger_id,
        candidate_artifact_hash,
        source_manifest.manifest_hash,
        config_bundle.manifest_hash,
    )
    dataset = build_candidate_evaluation_dataset_v2(
        dataset_id=dataset_id,
        challenger_id=challenger_id,
        candidate_artifact_hash=candidate_artifact_hash,
        source_manifest=source_manifest,
        eligible_instrument_count=(
            config.universe.eligible_instrument_count
        ),
        eligible_non_survivor_count=(
            config.universe.eligible_non_survivor_count
        ),
        scenarios=tuple(scenarios),
    )
    return ProspectiveEvaluationBuildResult(
        dataset=dataset,
        selected_request_count=required,
        terminal_failure_count=terminal_failure_count,
        request_coverage_ratio=coverage,
        base_scenario_count=required,
        variant_scenario_count=len(scenarios) - required,
        variant_coverage=dict(sorted(variant_coverage.items())),
        logical_created_at=selection_data_cutoff,
    )


def _validate_records(
    *,
    config_bundle: ProspectiveEvaluationConfigBundle,
    records: tuple[ProspectiveEvaluationRecord, ...],
) -> tuple[str, str]:
    config = config_bundle.config
    if not records:
        raise ProspectiveEvaluationError(
            "PROSPECTIVE_EVALUATION_RECORDS_EMPTY"
        )
    keys = tuple(
        (
            item.request.request.decision_time,
            item.request.prospective_request_id,
        )
        for item in records
    )
    if keys != tuple(sorted(set(keys))):
        raise ProspectiveEvaluationError(
            "PROSPECTIVE_EVALUATION_RECORDS_NOT_UNIQUE"
        )
    challenger_id = records[0].request.challenger_id
    artifact_hash = records[0].request.candidate_artifact_hash
    last_ordinal = -1
    for item in records:
        request = item.request
        execution = item.execution
        outcome = item.outcome
        market = item.market_inputs
        if (
            request.challenger_id != challenger_id
            or request.candidate_artifact_hash != artifact_hash
            or execution.status is not ProspectiveExecutionStatus.SUCCEEDED
            or execution.primary_response is None
            or not execution.deterministic_match
            or execution.prospective_request_id
            != request.prospective_request_id
            or execution.request_hash != request.request.request_hash
            or outcome.prospective_request_id
            != request.prospective_request_id
            or outcome.execution_id != execution.execution_id
            or outcome.execution_hash != execution.execution_hash
            or outcome.request_hash != request.request.request_hash
            or outcome.challenger_id != challenger_id
            or outcome.candidate_artifact_hash != artifact_hash
            or tuple(sorted(outcome.forward_returns))
            != config.universe.symbols
            or tuple(sorted(market.series)) != config.universe.symbols
            or market.signal_data_cutoff
            != request.request.signal_data_cutoff
            or market.session_dates
            != request.source_manifest.completed_session_dates
            or tuple(sorted(market.source_bar_ids))
            != tuple(
                sorted(
                    item.bar_id
                    for item in request.source_manifest.source_bars
                )
            )
            or len(item.calendar_path_hash) != 64
            or request.source_manifest.host_config_manifest_hash
            != config_bundle.prospective_manifest_hash
            or outcome.config_manifest_hash
            != config_bundle.outcome_manifest_hash
            or item.decision_session_ordinal <= last_ordinal
        ):
            raise ProspectiveEvaluationError(
                "PROSPECTIVE_EVALUATION_RECORD_BINDING_INVALID"
            )
        last_ordinal = item.decision_session_ordinal
    first_current = {
        item.symbol: item.current_weight
        for item in records[0].request.request.instruments
    }
    if any(value != 0 for value in first_current.values()):
        raise ProspectiveEvaluationError(
            "PROSPECTIVE_EVALUATION_INITIAL_STATE_NOT_CASH"
        )
    return challenger_id, artifact_hash


def _base_provenance(
    *,
    record: ProspectiveEvaluationRecord,
    config_bundle: ProspectiveEvaluationConfigBundle,
) -> tuple[str, str]:
    transformation_hash = canonical_hash(
        {
            "schema_version": (
                "prospective_evaluation_transformation_v2"
            ),
            "producer_version": config_bundle.config.producer_version,
            "config_manifest_hash": config_bundle.manifest_hash,
            "variant": CandidateEvaluationVariantV1(),
            "base_request_hash": record.request.request.request_hash,
            "base_source_manifest_hash": (
                record.request.source_manifest.manifest_hash
            ),
            "calendar_path_hash": record.calendar_path_hash,
        }
    )
    return (
        transformation_hash,
        stable_id(
            "candidate-evaluation-scenario-v2",
            record.request.prospective_request_id,
            BASE_VARIANT_ID,
            record.outcome.outcome_hash,
            transformation_hash,
        ),
    )


def _base_scenario(
    *,
    record: ProspectiveEvaluationRecord,
    config_bundle: ProspectiveEvaluationConfigBundle,
) -> tuple[
    CandidateEvaluationScenarioV1,
    CandidateEvaluationScenarioSourceBindingV2,
]:
    transformation_hash, scenario_id = _base_provenance(
        record=record,
        config_bundle=config_bundle,
    )
    scenario = build_candidate_evaluation_scenario(
        scenario_id=scenario_id,
        request=record.request.request,
        outcomes=build_candidate_outcomes(record.outcome),
        evaluation_nav_usd=record.outcome.evaluation_nav_usd,
    )
    return (
        scenario,
        build_candidate_evaluation_source_binding_v2(
            scenario=scenario,
            base_scenario_id=scenario_id,
            base_request_hash=record.request.request.request_hash,
            base_source_manifest_hash=(
                record.request.source_manifest.manifest_hash
            ),
            calendar_path_hash=record.calendar_path_hash,
            outcome_source_hash=record.outcome.outcome_hash,
            transformation_hash=transformation_hash,
        ),
    )


def _variant_scenarios(
    *,
    plan: _VariantPlan,
    records: tuple[ProspectiveEvaluationRecord, ...],
    config_bundle: ProspectiveEvaluationConfigBundle,
    state_executor: CandidateExecutor,
) -> tuple[
    tuple[
        CandidateEvaluationScenarioV1,
        CandidateEvaluationScenarioSourceBindingV2,
    ],
    ...,
]:
    first = records[0]
    current_weights = {
        item.symbol: item.current_weight
        for item in first.request.request.instruments
    }
    review_clock = _review_clock(first.request.request)
    if plan.review_clock_offset_sessions:
        review_clock = max(
            0,
            review_clock - plan.review_clock_offset_sessions,
        )
    previous_ordinal = first.decision_session_ordinal
    previous_response_hash: str | None = None
    built: list[
        tuple[
            CandidateEvaluationScenarioV1,
            CandidateEvaluationScenarioSourceBindingV2,
        ]
    ] = []
    for index, record in enumerate(records):
        if index:
            review_clock += (
                record.decision_session_ordinal - previous_ordinal
            )
        previous_ordinal = record.decision_session_ordinal
        if len(record.market_inputs.session_dates) < (
            plan.required_history_sessions
        ):
            continue
        request, transformation_hash = _variant_request(
            plan=plan,
            record=record,
            config_bundle=config_bundle,
            current_weights=current_weights,
            review_clock=review_clock,
            previous_response_hash=previous_response_hash,
        )
        try:
            response = CandidateDecisionResponseV1.model_validate(
                state_executor.execute(request)
            )
            response.assert_bound_to(request)
        except Exception as exc:
            raise ProspectiveEvaluationError(
                "PROSPECTIVE_EVALUATION_STATE_EXECUTION_FAILED:"
                + plan.variant_id
                + ":"
                + type(exc).__name__
            ) from None
        current_weights = {
            item.symbol: item.target_weight for item in response.targets
        }
        previous_response_hash = response.output_hash
        review_due = response.diagnostics.get("review_due")
        if not isinstance(review_due, bool):
            raise ProspectiveEvaluationError(
                "PROSPECTIVE_EVALUATION_REVIEW_DIAGNOSTIC_INVALID"
            )
        if review_due:
            review_clock = 0
        scenario_id = stable_id(
            "candidate-evaluation-scenario-v2",
            record.request.prospective_request_id,
            plan.variant_id,
            record.outcome.outcome_hash,
            transformation_hash,
        )
        _, base_scenario_id = _base_provenance(
            record=record,
            config_bundle=config_bundle,
        )
        scenario = build_candidate_evaluation_scenario(
            scenario_id=scenario_id,
            request=request,
            outcomes=build_candidate_outcomes(record.outcome),
            evaluation_nav_usd=record.outcome.evaluation_nav_usd,
        )
        built.append(
            (
                scenario,
                build_candidate_evaluation_source_binding_v2(
                    scenario=scenario,
                    base_scenario_id=base_scenario_id,
                    base_request_hash=(
                        record.request.request.request_hash
                    ),
                    base_source_manifest_hash=(
                        record.request.source_manifest.manifest_hash
                    ),
                    calendar_path_hash=record.calendar_path_hash,
                    outcome_source_hash=record.outcome.outcome_hash,
                    transformation_hash=transformation_hash,
                ),
            )
        )
    return tuple(built)


def _variant_request(
    *,
    plan: _VariantPlan,
    record: ProspectiveEvaluationRecord,
    config_bundle: ProspectiveEvaluationConfigBundle,
    current_weights: dict[str, float],
    review_clock: int,
    previous_response_hash: str | None,
) -> tuple[CandidateDecisionRequestV1, str]:
    base = record.request.request
    parameters = dict(config_bundle.strategy_parameters)
    parameters.update(plan.parameter_overrides)
    instruments = _variant_instruments(
        plan=plan,
        record=record,
        current_weights=current_weights,
        review_clock=review_clock,
            producer_version=config_bundle.config.producer_version,
            feature_calculation=(
                config_bundle.config.feature_calculation
            ),
            transform_constants=(
                config_bundle.config.transform_constants
            ),
    )
    feature_sources = {
        item.symbol: [feature.source_hash for feature in item.features]
        for item in instruments
    }
    transformation_payload = {
        "schema_version": "prospective_evaluation_transformation_v2",
        "producer_version": config_bundle.config.producer_version,
        "config_manifest_hash": config_bundle.manifest_hash,
        "variant": plan.variant,
        "base_request_hash": base.request_hash,
        "base_source_manifest_hash": (
            record.request.source_manifest.manifest_hash
        ),
        "strategy_parameters_hash": canonical_hash(parameters),
        "current_weights": dict(sorted(current_weights.items())),
        "review_clock": review_clock,
        "decision_session_ordinal": record.decision_session_ordinal,
        "calendar_path_hash": record.calendar_path_hash,
        "previous_response_hash": previous_response_hash,
        "feature_source_hashes": feature_sources,
    }
    transformation_hash = canonical_hash(transformation_payload)
    request_id = stable_id(
        "candidate-evaluation-request-v2",
        base.request_id,
        plan.variant_id,
        transformation_hash,
    )
    return (
        build_candidate_decision_request(
            request_id=request_id,
            challenger_id=base.challenger_id,
            candidate_artifact_hash=base.candidate_artifact_hash,
            strategy_id=base.strategy_id,
            strategy_version=base.strategy_version,
            decision_time=base.decision_time,
            signal_data_cutoff=base.signal_data_cutoff,
            variant=plan.variant,
            instruments=instruments,
            constraints=base.constraints,
            strategy_parameters=parameters,
            source_data_manifest_hash=transformation_hash,
        ),
        transformation_hash,
    )


def _variant_instruments(
    *,
    plan: _VariantPlan,
    record: ProspectiveEvaluationRecord,
    current_weights: dict[str, float],
    review_clock: int,
    producer_version: str,
    feature_calculation: ProspectiveEvaluationFeatureCalculationV1,
    transform_constants: ProspectiveEvaluationTransformConstantsV1,
) -> tuple[CandidateInstrumentInputV1, ...]:
    base_by_symbol = {
        item.symbol: item for item in record.request.request.instruments
    }
    features_by_symbol: dict[str, list[CandidateFeatureValueV1]] = {}
    for symbol, base in sorted(base_by_symbol.items()):
        if plan.parameter_overrides and _changes_feature_windows(plan):
            price_features = build_candidate_price_features(
                series=record.market_inputs.series[symbol],
                qqq_series=record.market_inputs.series["QQQ"],
                short_return_sessions=int(
                    plan.parameter_overrides.get(
                        "short_return_sessions",
                        _feature_window_value(base, "total_return_", 0),
                    )
                ),
                long_return_sessions=int(
                    plan.parameter_overrides.get(
                        "long_return_sessions",
                        _feature_window_value(base, "total_return_", 1),
                    )
                ),
                moving_average_sessions=int(
                    plan.parameter_overrides.get(
                        "moving_average_sessions",
                        _single_feature_window(
                            base,
                            "moving_average_gap_",
                        ),
                    )
                ),
                realized_volatility_sessions=int(
                    plan.parameter_overrides.get(
                        "short_return_sessions",
                        _single_feature_window(
                            base,
                            "realized_volatility_",
                        ),
                    )
                ),
                downside_beta_sessions=int(
                    plan.parameter_overrides.get(
                        "downside_beta_sessions",
                        _single_feature_window(
                            base,
                            "downside_beta_",
                            suffix="_qqq",
                        ),
                    )
                ),
                annualization_sessions=(
                    feature_calculation.annualization_sessions
                ),
                realized_volatility_ddof=(
                    feature_calculation.realized_volatility_ddof
                ),
                minimum_variance=feature_calculation.minimum_variance,
                source_revision=feature_calculation.source_revision,
                formula_version=(
                    producer_version + ":" + plan.variant_id
                ),
            )
            carried = [
                item
                for item in base.features
                if not _is_price_feature(item.name)
            ]
            features = [*price_features, *carried]
        else:
            features = list(base.features)
        features_by_symbol[symbol] = features

    for symbol, features in features_by_symbol.items():
        features_by_symbol[symbol] = _replace_review_clock(
            features,
            value=review_clock,
            variant_id=plan.variant_id,
            symbol=symbol,
        )
    if plan.ablation_symbol is not None:
        features_by_symbol[plan.ablation_symbol] = [
            item
            for item in features_by_symbol[plan.ablation_symbol]
            if not _is_price_feature(item.name)
        ]
    if plan.placebo_transform is not None:
        _apply_placebo(
            features_by_symbol,
            transform=plan.placebo_transform,
            variant_id=plan.variant_id,
            constants=transform_constants,
        )
    if plan.shuffle_pair is not None:
        _apply_symbol_shuffle(
            features_by_symbol,
            left=plan.shuffle_pair[0],
            right=plan.shuffle_pair[1],
            variant_id=plan.variant_id,
        )

    return tuple(
        CandidateInstrumentInputV1(
            symbol=symbol,
            current_weight=current_weights[symbol],
            membership_available_at=base_by_symbol[
                symbol
            ].membership_available_at,
            membership_valid_from=base_by_symbol[
                symbol
            ].membership_valid_from,
            membership_valid_until=base_by_symbol[
                symbol
            ].membership_valid_until,
            instrument_is_non_survivor=base_by_symbol[
                symbol
            ].instrument_is_non_survivor,
            features=tuple(
                sorted(
                    features_by_symbol[symbol],
                    key=lambda item: item.name,
                )
            ),
        )
        for symbol in sorted(base_by_symbol)
    )


def build_candidate_outcomes(
    evidence: ProspectiveOutcomeEvidenceV1,
) -> tuple[CandidateOutcomeV1, ...]:
    known_factors = tuple(
        KnownFactorReturnV1(
            factor_id=item.factor_id,
            return_value=item.return_value,
        )
        for item in evidence.known_factor_returns
    )
    return tuple(
        CandidateOutcomeV1(
            symbol=symbol,
            trade_id=stable_id(
                "prospective-evaluation-trade",
                evidence.outcome_id,
                symbol,
            ),
            forward_return=evidence.forward_returns[symbol],
            baseline_current_weight=(
                evidence.baseline_current_weights[symbol]
            ),
            baseline_target_weight=(
                evidence.baseline_target_weights[symbol]
            ),
            commission_bps=evidence.commission_bps,
            spread_bps=evidence.spread_bps,
            delay_bps=evidence.delay_bps,
            adv_usd=evidence.adv_usd[symbol],
            market_return=evidence.market_return,
            sector_return=evidence.sector_return,
            known_factor_returns=known_factors,
            regime=evidence.regime,
            outcome_available_at=evidence.outcome_available_at,
        )
        for symbol in sorted(evidence.forward_returns)
    )


def _variant_plans(
    config: ProspectiveEvaluationConfigV1,
) -> tuple[_VariantPlan, ...]:
    plans: list[_VariantPlan] = []
    for item in config.parameter_neighborhoods:
        plans.append(
            _VariantPlan(
                variant=CandidateEvaluationVariantV1(
                    parameter_neighborhood_id=item.variant_id
                ),
                required_history_sessions=item.required_history_sessions,
                parameter_overrides=dict(item.parameter_overrides),
            )
        )
    for item in config.data_ablations:
        plans.append(
            _VariantPlan(
                variant=CandidateEvaluationVariantV1(
                    data_ablation_id=item.variant_id
                ),
                required_history_sessions=1,
                parameter_overrides={},
                ablation_symbol=item.symbol,
            )
        )
    for item in config.date_shifts:
        plans.append(
            _VariantPlan(
                variant=CandidateEvaluationVariantV1(
                    date_shift_id=item.variant_id
                ),
                required_history_sessions=1,
                parameter_overrides={},
                review_clock_offset_sessions=(
                    item.review_clock_offset_sessions
                ),
            )
        )
    for item in config.placebos:
        plans.append(
            _VariantPlan(
                variant=CandidateEvaluationVariantV1(
                    inversion_id=item.variant_id
                ),
                required_history_sessions=1,
                parameter_overrides={},
                placebo_transform=item.transform,
            )
        )
    for item in config.symbol_shuffles:
        plans.append(
            _VariantPlan(
                variant=CandidateEvaluationVariantV1(
                    shuffle_id=item.variant_id
                ),
                required_history_sessions=1,
                parameter_overrides={},
                shuffle_pair=(item.left_symbol, item.right_symbol),
            )
        )
    return tuple(sorted(plans, key=lambda item: item.variant.key))


def _review_clock(request: CandidateDecisionRequestV1) -> int:
    for instrument in request.instruments:
        for feature in instrument.features:
            if feature.name == "completed_sessions_since_review":
                value = feature.value
                if value >= 0 and value.is_integer():
                    return int(value)
    raise ProspectiveEvaluationError(
        "PROSPECTIVE_EVALUATION_REVIEW_CLOCK_MISSING"
    )


def _replace_review_clock(
    features: list[CandidateFeatureValueV1],
    *,
    value: int,
    variant_id: str,
    symbol: str,
) -> list[CandidateFeatureValueV1]:
    existing = next(
        (
            item
            for item in features
            if item.name == "completed_sessions_since_review"
        ),
        None,
    )
    if existing is None:
        raise ProspectiveEvaluationError(
            "PROSPECTIVE_EVALUATION_REVIEW_CLOCK_MISSING"
        )
    replacement = _transformed_feature(
        existing,
        value=float(value),
        variant_id=variant_id,
        transform="REVIEW_CLOCK",
        symbol=symbol,
    )
    return [
        replacement
        if item.name == "completed_sessions_since_review"
        else item
        for item in features
    ]


def _apply_placebo(
    features_by_symbol: dict[str, list[CandidateFeatureValueV1]],
    *,
    transform: str,
    variant_id: str,
    constants: ProspectiveEvaluationTransformConstantsV1,
) -> None:
    if transform == "SGOV_ONLY":
        for symbol in ("GLD", "TLT"):
            features_by_symbol[symbol] = [
                item
                for item in features_by_symbol[symbol]
                if not _is_price_feature(item.name)
            ]
        return
    if transform == "INVERT_EXCESS_TREND":
        reserve = {
            item.name: item
            for item in features_by_symbol["SGOV"]
        }
        for symbol in ("GLD", "TLT"):
            transformed: list[CandidateFeatureValueV1] = []
            for feature in features_by_symbol[symbol]:
                value = feature.value
                if feature.name.startswith("total_return_"):
                    reserve_feature = reserve.get(feature.name)
                    if reserve_feature is None:
                        raise ProspectiveEvaluationError(
                            "PROSPECTIVE_EVALUATION_RESERVE_FEATURE_MISSING"
                        )
                    value = 2 * reserve_feature.value - feature.value
                elif feature.name.startswith("moving_average_gap_"):
                    value = -feature.value
                transformed.append(
                    _transformed_feature(
                        feature,
                        value=value,
                        variant_id=variant_id,
                        transform=transform,
                        symbol=symbol,
                    )
                    if value != feature.value
                    else feature
                )
            features_by_symbol[symbol] = transformed
        return
    if transform == "REMOVE_DOWNSIDE_BETA_GATE":
        for symbol in ("GLD", "TLT"):
            features_by_symbol[symbol] = [
                (
                    _transformed_feature(
                        item,
                        value=constants.removed_downside_beta,
                        variant_id=variant_id,
                        transform=transform,
                        symbol=symbol,
                    )
                    if item.name.startswith("downside_beta_")
                    else item
                )
                for item in features_by_symbol[symbol]
            ]
        return
    if transform == "STATIC_GLD_TLT":
        reserve = {
            item.name: item
            for item in features_by_symbol["SGOV"]
        }
        for symbol in ("GLD", "TLT"):
            transformed = []
            for item in features_by_symbol[symbol]:
                value = item.value
                if item.name.startswith("total_return_"):
                    source = reserve.get(item.name)
                    if source is None:
                        raise ProspectiveEvaluationError(
                            "PROSPECTIVE_EVALUATION_RESERVE_FEATURE_MISSING"
                        )
                    value = (
                        source.value + constants.static_excess_return
                    )
                elif item.name.startswith("moving_average_gap_"):
                    value = constants.static_moving_average_gap
                elif item.name.startswith("realized_volatility_"):
                    value = constants.static_realized_volatility
                elif item.name.startswith("downside_beta_"):
                    value = constants.static_downside_beta
                elif item.name.startswith(
                    "downside_observation_count_"
                ):
                    value = max(
                        float(
                            constants.static_minimum_downside_observations
                        ),
                        item.value,
                    )
                transformed.append(
                    _transformed_feature(
                        item,
                        value=value,
                        variant_id=variant_id,
                        transform=transform,
                        symbol=symbol,
                    )
                    if value != item.value
                    else item
                )
            features_by_symbol[symbol] = transformed
        return
    raise ProspectiveEvaluationError(
        "PROSPECTIVE_EVALUATION_PLACEBO_UNKNOWN"
    )


def _apply_symbol_shuffle(
    features_by_symbol: dict[str, list[CandidateFeatureValueV1]],
    *,
    left: str,
    right: str,
    variant_id: str,
) -> None:
    left_features = {
        item.name: item
        for item in features_by_symbol[left]
        if _is_price_feature(item.name)
    }
    right_features = {
        item.name: item
        for item in features_by_symbol[right]
        if _is_price_feature(item.name)
    }
    if set(left_features) != set(right_features):
        raise ProspectiveEvaluationError(
            "PROSPECTIVE_EVALUATION_SHUFFLE_FEATURES_MISMATCH"
        )
    for destination, source, source_symbol in (
        (left, right_features, right),
        (right, left_features, left),
    ):
        kept = [
            item
            for item in features_by_symbol[destination]
            if not _is_price_feature(item.name)
        ]
        swapped = [
            _transformed_feature(
                item,
                value=item.value,
                variant_id=variant_id,
                transform="SYMBOL_LABEL_SHUFFLE",
                symbol=destination,
                extra={"source_symbol": source_symbol},
            )
            for item in source.values()
        ]
        features_by_symbol[destination] = [*kept, *swapped]


def _transformed_feature(
    feature: CandidateFeatureValueV1,
    *,
    value: float,
    variant_id: str,
    transform: str,
    symbol: str,
    extra: dict[str, object] | None = None,
) -> CandidateFeatureValueV1:
    if not math.isfinite(value):
        raise ProspectiveEvaluationError(
            "PROSPECTIVE_EVALUATION_TRANSFORM_NONFINITE"
        )
    payload = feature.model_dump(mode="python")
    payload["value"] = float(value)
    payload["source_hash"] = canonical_hash(
        {
            "schema_version": (
                "prospective_evaluation_feature_transform_v2"
            ),
            "variant_id": variant_id,
            "transform": transform,
            "symbol": symbol,
            "feature_name": feature.name,
            "value": float(value),
            "source_hash": feature.source_hash,
            **(extra or {}),
        }
    )
    return CandidateFeatureValueV1.model_validate(payload)


def _is_price_feature(name: str) -> bool:
    return name.startswith(_PRICE_FEATURE_PREFIXES)


def _changes_feature_windows(plan: _VariantPlan) -> bool:
    return bool(
        {
            "short_return_sessions",
            "long_return_sessions",
            "moving_average_sessions",
            "downside_beta_sessions",
        }
        & set(plan.parameter_overrides)
    )


def _feature_window_value(
    instrument: CandidateInstrumentInputV1,
    prefix: str,
    index: int,
) -> int:
    windows = sorted(
        int(item.name.removeprefix(prefix))
        for item in instrument.features
        if item.name.startswith(prefix)
    )
    if len(windows) < 2:
        raise ProspectiveEvaluationError(
            "PROSPECTIVE_EVALUATION_FEATURE_WINDOW_MISSING"
        )
    return windows[index]


def _single_feature_window(
    instrument: CandidateInstrumentInputV1,
    prefix: str,
    *,
    suffix: str = "",
) -> int:
    matches = [
        item.name
        for item in instrument.features
        if item.name.startswith(prefix)
        and (not suffix or item.name.endswith(suffix))
    ]
    if len(matches) != 1:
        raise ProspectiveEvaluationError(
            "PROSPECTIVE_EVALUATION_FEATURE_WINDOW_MISSING"
        )
    value = matches[0].removeprefix(prefix)
    if suffix:
        value = value.removesuffix(suffix)
    return int(value)


def _validate_strategy_variants(
    *,
    config: ProspectiveEvaluationConfigV1,
    strategy_document: dict[str, JsonValue],
) -> None:
    parameters = strategy_document.get("parameters")
    falsification = strategy_document.get("falsification")
    if not isinstance(parameters, dict) or not isinstance(
        falsification,
        dict,
    ):
        raise ValueError(
            "prospective evaluation strategy contract is incomplete"
        )
    expected_parameter_overrides: list[dict[str, int | float]] = []
    base_short = cast(int, parameters["short_return_sessions"])
    base_long = cast(int, parameters["long_return_sessions"])
    for pair in cast(list[list[int]], falsification["return_horizon_grid"]):
        if pair != [base_short, base_long]:
            expected_parameter_overrides.append(
                {
                    "long_return_sessions": pair[1],
                    "short_return_sessions": pair[0],
                }
            )
    for field, grid_name in (
        ("moving_average_sessions", "moving_average_grid"),
        ("sleeve_cap", "sleeve_cap_grid"),
        (
            "gld_entry_downside_beta",
            "gld_entry_downside_beta_grid",
        ),
        (
            "tlt_entry_downside_beta",
            "tlt_entry_downside_beta_grid",
        ),
    ):
        base = parameters[field]
        for value in cast(list[int | float], falsification[grid_name]):
            if value != base:
                expected_parameter_overrides.append({field: value})
    actual = {
        canonical_hash(item.parameter_overrides)
        for item in config.parameter_neighborhoods
    }
    expected = {
        canonical_hash(dict(sorted(item.items())))
        for item in expected_parameter_overrides
    }
    if actual != expected:
        raise ValueError(
            "prospective parameter variants differ from strategy grid"
        )
    expected_ablations = set(
        cast(list[str], falsification["ablations"])
    )
    if {
        item.variant_id for item in config.data_ablations
    } != expected_ablations:
        raise ValueError(
            "prospective data ablations differ from strategy contract"
        )
    expected_placebos = set(cast(list[str], falsification["placebos"]))
    actual_placebos = {
        item.variant_id for item in config.placebos
    } | {item.variant_id for item in config.date_shifts}
    if actual_placebos != expected_placebos:
        raise ValueError(
            "prospective placebos differ from strategy contract"
        )
