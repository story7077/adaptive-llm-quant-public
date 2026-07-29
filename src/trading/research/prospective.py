from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

import yaml
from pydantic import Field, JsonValue, TypeAdapter, field_validator, model_validator

from trading.data.q1_pit import AlignedDailyInputs, CompletedDailySeries
from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.q1 import Q1ArmId, Q1StrategyDecision, StrategyEvaluationAnchor
from trading.domain.time import require_aware_utc
from trading.research.candidate_abi import (
    CandidateDecisionConstraintsV1,
    CandidateDecisionRequestV1,
    CandidateDecisionResponseV1,
    CandidateEvaluationVariantV1,
    CandidateFeatureValueV1,
    CandidateInstrumentInputV1,
    build_candidate_decision_request,
)
from trading.research.candidate_artifact import CandidateArtifactBundleV1
from trading.research.candidate_process import CandidateExecutionSecurityV1
from trading.research.commander_candidate import CandidateRuntimeAttestationV1
from trading.research.contracts import (
    HASH_PATTERN,
    IDENTIFIER_PATTERN,
    SYMBOL_PATTERN,
    VERSION_PATTERN,
)

PROSPECTIVE_CONFIG_FILE = "research/candidate-prospective.yaml"
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class ProspectiveCandidateError(RuntimeError):
    """Fail-closed prospective collection error."""


class ProspectiveMarketDataConfigV1(DomainModel):
    provider: Literal["alpaca"]
    feed: Literal["iex"]
    timeframe: Literal["1Day"]
    adjustment: Literal["all"]
    dataset_version: str = Field(min_length=1, max_length=120)
    required_completed_sessions: int = Field(gt=1)
    query_limit: int = Field(gt=1)

    @model_validator(mode="after")
    def validate_query_limit(self) -> Self:
        if self.query_limit < self.required_completed_sessions:
            raise ValueError("prospective query limit is smaller than required history")
        return self


class ProspectiveFeatureConfigV1(DomainModel):
    total_return_short_sessions: int = Field(gt=1)
    total_return_long_sessions: int = Field(gt=1)
    moving_average_sessions: int = Field(gt=1)
    realized_volatility_sessions: int = Field(gt=1)
    downside_beta_sessions: int = Field(gt=1)
    annualization_sessions: int = Field(gt=1)
    realized_volatility_ddof: int = Field(ge=0)
    minimum_variance: float = Field(gt=0)
    source_revision: int = Field(ge=0)
    initial_completed_sessions_since_review: int = Field(ge=0)

    @field_validator("minimum_variance", mode="after")
    @classmethod
    def validate_finite_variance(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("prospective minimum variance must be finite")
        return value

    @model_validator(mode="after")
    def validate_windows(self) -> Self:
        if self.total_return_short_sessions >= self.total_return_long_sessions:
            raise ValueError("prospective return windows must be strictly ordered")
        if self.realized_volatility_ddof >= self.realized_volatility_sessions:
            raise ValueError("prospective volatility ddof exhausts the sample")
        return self


class ProspectiveMembershipConfigV1(DomainModel):
    available_at: datetime
    valid_from: datetime
    valid_until: datetime | None
    instrument_is_non_survivor: bool

    @field_validator(
        "available_at",
        "valid_from",
        "valid_until",
        mode="after",
    )
    @classmethod
    def validate_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware_utc(value)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.valid_until is not None and self.valid_until < self.valid_from:
            raise ValueError("prospective membership window is reversed")
        if self.instrument_is_non_survivor and self.valid_until is None:
            raise ValueError("non-survivor membership requires a valid_until")
        return self


class ProspectiveStateConfigV1(DomainModel):
    initial_state: Literal["CASH_ONLY_AT_EVALUATION_ANCHOR"]
    subsequent_state: Literal["PRIOR_VERIFIED_TARGETS"]
    decision_time_source: Literal["PARENT_DECISION_CREATED_AT"]


class ProspectiveOperationsConfigV1(DomainModel):
    watch_poll_seconds: int = Field(gt=0, le=60)
    maximum_wait_seconds: int = Field(gt=0)


class ProspectiveSafetyConfigV1(DomainModel):
    real_order_routing: Literal[False] = False
    automatic_promotion_enabled: Literal[False] = False
    challenger_lifecycle_advance_enabled: Literal[False] = False
    shadow_activation_enabled: Literal[False] = False
    broker_access_permitted: Literal[False] = False


class ProspectiveCandidateConfigV1(DomainModel):
    schema_version: Literal["candidate_prospective_config_v1"]
    producer_version: str = Field(pattern=VERSION_PATTERN)
    strategy_config_path: str = Field(min_length=1, max_length=240)
    strategy_config_content_sha256: str = Field(pattern=HASH_PATTERN)
    strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    strategy_version: str = Field(pattern=VERSION_PATTERN)
    reference_universe: tuple[str, ...] = Field(min_length=1)
    market_data: ProspectiveMarketDataConfigV1
    features: ProspectiveFeatureConfigV1
    membership: ProspectiveMembershipConfigV1
    constraints: CandidateDecisionConstraintsV1
    state: ProspectiveStateConfigV1
    operations: ProspectiveOperationsConfigV1
    safety: ProspectiveSafetyConfigV1

    @field_validator("reference_universe", mode="after")
    @classmethod
    def validate_universe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("prospective reference universe must be unique and sorted")
        if any(not _valid_symbol(symbol) for symbol in value):
            raise ValueError("prospective reference universe contains an invalid symbol")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        path = PurePosixPath(self.strategy_config_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("prospective strategy config path must stay inside config")
        if tuple(self.constraints.maximum_weight_by_symbol) != self.reference_universe:
            raise ValueError("prospective constraints differ from reference universe")
        required = max(
            self.features.total_return_long_sessions + 1,
            self.features.moving_average_sessions,
            self.features.realized_volatility_sessions + 1,
            self.features.downside_beta_sessions + 1,
        )
        if self.market_data.required_completed_sessions < required:
            raise ValueError("prospective history cannot satisfy feature windows")
        return self


@dataclass(frozen=True, slots=True)
class ProspectiveCandidateConfigBundle:
    config: ProspectiveCandidateConfigV1
    strategy_parameters: dict[str, JsonValue]
    strategy_document: dict[str, JsonValue]
    strategy_document_hash: str
    manifest_hash: str
    path: Path
    strategy_path: Path


class ProspectiveSourceBarV1(DomainModel):
    bar_id: str = Field(pattern=IDENTIFIER_PATTERN)
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    session_date: date
    source_event_time: datetime
    available_at: datetime
    payload_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("source_event_time", "available_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.source_event_time > self.available_at:
            raise ValueError("prospective source bar is available before its event")
        return self


class ProspectiveSourceManifestV1(DomainModel):
    schema_version: Literal["candidate_prospective_source_manifest_v1"]
    producer_version: str = Field(pattern=VERSION_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    parent_run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parent_portfolio_decision_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parent_decision_hash: str = Field(pattern=HASH_PATTERN)
    parent_input_manifest_hash: str = Field(pattern=HASH_PATTERN)
    parent_scheduled_at: datetime
    evaluation_anchor_id: str = Field(pattern=IDENTIFIER_PATTERN)
    evaluation_anchor_hash: str = Field(pattern=HASH_PATTERN)
    prior_prospective_request_id: str | None = Field(
        default=None,
        pattern=IDENTIFIER_PATTERN,
    )
    prior_execution_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    state_source: Literal[
        "CASH_ONLY_AT_EVALUATION_ANCHOR",
        "PRIOR_VERIFIED_TARGETS",
    ]
    market_dataset_version: str = Field(min_length=1, max_length=120)
    signal_data_cutoff: datetime
    completed_session_dates: tuple[date, ...] = Field(min_length=1)
    source_bars: tuple[ProspectiveSourceBarV1, ...] = Field(min_length=1)
    formula_contract_hash: str = Field(pattern=HASH_PATTERN)
    host_config_manifest_hash: str = Field(pattern=HASH_PATTERN)
    manifest_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("parent_scheduled_at", "signal_data_cutoff", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.completed_session_dates != tuple(
            sorted(set(self.completed_session_dates))
        ):
            raise ValueError("prospective completed sessions must be unique and sorted")
        bar_keys = tuple(
            (item.symbol, item.session_date, item.bar_id) for item in self.source_bars
        )
        if bar_keys != tuple(sorted(set(bar_keys))):
            raise ValueError("prospective source bars must be unique and sorted")
        if any(item.available_at > self.signal_data_cutoff for item in self.source_bars):
            raise ValueError("future source bar entered prospective manifest")
        if {
            item.session_date for item in self.source_bars
        } != set(self.completed_session_dates):
            raise ValueError("prospective source bars and sessions are not aligned")
        if self.state_source == "CASH_ONLY_AT_EVALUATION_ANCHOR" and (
            self.prior_prospective_request_id is not None
            or self.prior_execution_hash is not None
        ):
            raise ValueError("initial prospective state cannot cite prior execution")
        if self.state_source == "PRIOR_VERIFIED_TARGETS" and (
            self.prior_prospective_request_id is None
            or self.prior_execution_hash is None
        ):
            raise ValueError("subsequent prospective state requires prior execution")
        payload = self.model_dump(mode="python", exclude={"manifest_hash"})
        if canonical_hash(payload) != self.manifest_hash:
            raise ValueError("prospective source manifest hash mismatch")
        return self


class ProspectiveRequestEvidenceV1(DomainModel):
    schema_version: Literal["candidate_prospective_request_evidence_v1"]
    prospective_request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_bundle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    candidate_config_hash: str = Field(pattern=HASH_PATTERN)
    strategy_config_content_sha256: str = Field(pattern=HASH_PATTERN)
    parent_run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parent_portfolio_decision_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parent_scheduled_at: datetime
    calendar_session_id: str = Field(pattern=IDENTIFIER_PATTERN)
    evaluation_anchor_id: str = Field(pattern=IDENTIFIER_PATTERN)
    prior_prospective_request_id: str | None = Field(
        default=None,
        pattern=IDENTIFIER_PATTERN,
    )
    source_manifest: ProspectiveSourceManifestV1
    request: CandidateDecisionRequestV1
    created_at: datetime
    real_order_routing: Literal[False] = False
    automatic_promotion_enabled: Literal[False] = False
    challenger_lifecycle_advance_enabled: Literal[False] = False
    shadow_activation_enabled: Literal[False] = False
    evidence_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("parent_scheduled_at", "created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if (
            self.prospective_request_id != self.request.request_id
            or self.challenger_id != self.request.challenger_id
            or self.candidate_artifact_hash
            != self.request.candidate_artifact_hash
            or self.request.source_data_manifest_hash
            != self.source_manifest.manifest_hash
            or self.parent_run_id != self.source_manifest.parent_run_id
            or self.parent_portfolio_decision_id
            != self.source_manifest.parent_portfolio_decision_id
            or self.parent_scheduled_at
            != self.source_manifest.parent_scheduled_at
            or self.evaluation_anchor_id
            != self.source_manifest.evaluation_anchor_id
            or self.prior_prospective_request_id
            != self.source_manifest.prior_prospective_request_id
        ):
            raise ValueError("prospective request evidence binding mismatch")
        if self.created_at != self.request.decision_time:
            raise ValueError("prospective logical creation time must equal decision time")
        payload = self.model_dump(mode="python", exclude={"evidence_hash"})
        if canonical_hash(payload) != self.evidence_hash:
            raise ValueError("prospective request evidence hash mismatch")
        return self


class ProspectiveExecutionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ProspectiveExecutionEvidenceV1(DomainModel):
    schema_version: Literal["candidate_prospective_execution_evidence_v1"]
    execution_id: str = Field(pattern=IDENTIFIER_PATTERN)
    prospective_request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    request_hash: str = Field(pattern=HASH_PATTERN)
    status: ProspectiveExecutionStatus
    runtime_attestation_hash: str = Field(pattern=HASH_PATTERN)
    security_contract_hash: str = Field(pattern=HASH_PATTERN)
    primary_response: CandidateDecisionResponseV1 | None
    replay_response: CandidateDecisionResponseV1 | None
    deterministic_match: bool
    error_code: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    created_at: datetime
    real_order_routing: Literal[False] = False
    evidence_recorded: Literal[True] = True
    challenger_status_advanced: Literal[False] = False
    shadow_started: Literal[False] = False
    execution_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_execution(self) -> Self:
        if self.status == ProspectiveExecutionStatus.SUCCEEDED:
            if (
                self.primary_response is None
                or self.replay_response is None
                or self.error_code is not None
                or not self.deterministic_match
                or self.primary_response.output_hash
                != self.replay_response.output_hash
            ):
                raise ValueError("successful prospective execution is incomplete")
        elif (
            self.primary_response is not None
            or self.replay_response is not None
            or self.error_code is None
            or self.deterministic_match
        ):
            raise ValueError("failed prospective execution has unsafe output")
        payload = self.model_dump(mode="python", exclude={"execution_hash"})
        if canonical_hash(payload) != self.execution_hash:
            raise ValueError("prospective execution hash mismatch")
        return self


@dataclass(frozen=True, slots=True)
class PriorProspectiveState:
    request_id: str
    execution_hash: str
    target_weights: dict[str, float]
    completed_sessions_since_review: int


def load_prospective_candidate_config(
    config_dir: Path,
) -> ProspectiveCandidateConfigBundle:
    path = (config_dir / PROSPECTIVE_CONFIG_FILE).resolve(strict=True)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("candidate prospective config must be a YAML object")
    config = ProspectiveCandidateConfigV1.model_validate(loaded)
    strategy_path = (config_dir / config.strategy_config_path).resolve(strict=True)
    if not strategy_path.is_relative_to(config_dir.resolve(strict=True)):
        raise ValueError("prospective strategy config escaped config root")
    raw_strategy = strategy_path.read_bytes()
    try:
        strategy_text = raw_strategy.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("prospective strategy config is not UTF-8") from exc
    normalized_strategy = strategy_text.replace("\r\n", "\n").replace("\r", "\n")
    if (
        hashlib.sha256(normalized_strategy.encode("utf-8")).hexdigest()
        != config.strategy_config_content_sha256
    ):
        raise ValueError("prospective strategy config content SHA-256 mismatch")
    loaded_strategy = yaml.safe_load(strategy_text)
    if not isinstance(loaded_strategy, dict):
        raise ValueError("prospective strategy config must be a YAML object")
    strategy_document = _JSON_OBJECT_ADAPTER.validate_python(loaded_strategy)
    if (
        strategy_document.get("strategy_id") != config.strategy_id
        or strategy_document.get("strategy_version") != config.strategy_version
        or tuple(cast(list[str], strategy_document.get("reference_universe")))
        != config.reference_universe
    ):
        raise ValueError("prospective strategy identity mismatch")
    parameters = strategy_document.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("prospective strategy parameters are missing")
    strategy_parameters = _JSON_OBJECT_ADAPTER.validate_python(parameters)
    _validate_parameter_alignment(config, strategy_parameters)
    strategy_document_hash = canonical_hash(strategy_document)
    manifest_hash = canonical_hash(
        {
            PROSPECTIVE_CONFIG_FILE: config,
            config.strategy_config_path: strategy_document,
        }
    )
    return ProspectiveCandidateConfigBundle(
        config=config,
        strategy_parameters=strategy_parameters,
        strategy_document=strategy_document,
        strategy_document_hash=strategy_document_hash,
        manifest_hash=manifest_hash,
        path=path,
        strategy_path=strategy_path,
    )


def build_prospective_request_evidence(
    *,
    config_bundle: ProspectiveCandidateConfigBundle,
    artifact: CandidateArtifactBundleV1,
    parent_decision: Q1StrategyDecision,
    evaluation_anchor: StrategyEvaluationAnchor,
    market_inputs: AlignedDailyInputs,
    prior_state: PriorProspectiveState | None,
) -> ProspectiveRequestEvidenceV1:
    config = config_bundle.config
    if parent_decision.arm_id is not Q1ArmId.Q1_DET:
        raise ProspectiveCandidateError("PARENT_DECISION_ARM_INVALID")
    if parent_decision.run_id != evaluation_anchor.run_id:
        raise ProspectiveCandidateError("PARENT_ANCHOR_RUN_MISMATCH")
    if artifact.challenger_id == "":
        raise ProspectiveCandidateError("CANDIDATE_ARTIFACT_INVALID")
    if (
        parent_decision.decision_created_at
        < config.membership.available_at
        or parent_decision.decision_created_at
        < config.membership.valid_from
        or (
            config.membership.valid_until is not None
            and parent_decision.decision_created_at
            > config.membership.valid_until
        )
    ):
        raise ProspectiveCandidateError("REFERENCE_UNIVERSE_NOT_EFFECTIVE")
    if tuple(sorted(market_inputs.series)) != config.reference_universe:
        raise ProspectiveCandidateError("PROSPECTIVE_UNIVERSE_MISMATCH")
    if market_inputs.signal_data_cutoff > parent_decision.decision_created_at:
        raise ProspectiveCandidateError("PROSPECTIVE_MARKET_CUTOFF_AFTER_PARENT")

    current_weights = (
        {symbol: 0.0 for symbol in config.reference_universe}
        if prior_state is None
        else dict(prior_state.target_weights)
    )
    if tuple(sorted(current_weights)) != config.reference_universe:
        raise ProspectiveCandidateError("PROSPECTIVE_STATE_UNIVERSE_MISMATCH")
    if any(
        not math.isfinite(weight) or weight < 0.0
        for weight in current_weights.values()
    ):
        raise ProspectiveCandidateError("PROSPECTIVE_STATE_INVALID")
    if (
        sum(current_weights.values())
        > config.constraints.maximum_gross_weight
        + config.constraints.numeric_tolerance
    ):
        raise ProspectiveCandidateError("PROSPECTIVE_STATE_LEVERAGED")

    source_bars = _source_bars(market_inputs)
    formula_contract_hash = canonical_hash(
        {
            "producer_version": config.producer_version,
            "feature_contract": config.features,
            "market_data_contract": config.market_data,
        }
    )
    source_payload = {
        "schema_version": "candidate_prospective_source_manifest_v1",
        "producer_version": config.producer_version,
        "challenger_id": artifact.challenger_id,
        "candidate_artifact_hash": artifact.bundle_hash,
        "parent_run_id": parent_decision.run_id,
        "parent_portfolio_decision_id": parent_decision.portfolio_decision_id,
        "parent_decision_hash": parent_decision.decision_hash,
        "parent_input_manifest_hash": parent_decision.input_manifest.manifest_hash,
        "parent_scheduled_at": parent_decision.scheduled_at,
        "evaluation_anchor_id": evaluation_anchor.evaluation_anchor_id,
        "evaluation_anchor_hash": evaluation_anchor.anchor_hash,
        "prior_prospective_request_id": (
            None if prior_state is None else prior_state.request_id
        ),
        "prior_execution_hash": (
            None if prior_state is None else prior_state.execution_hash
        ),
        "state_source": (
            config.state.initial_state
            if prior_state is None
            else config.state.subsequent_state
        ),
        "market_dataset_version": config.market_data.dataset_version,
        "signal_data_cutoff": parent_decision.decision_created_at,
        "completed_session_dates": market_inputs.session_dates,
        "source_bars": source_bars,
        "formula_contract_hash": formula_contract_hash,
        "host_config_manifest_hash": config_bundle.manifest_hash,
    }
    source_manifest = ProspectiveSourceManifestV1.model_validate(
        {**source_payload, "manifest_hash": canonical_hash(source_payload)}
    )
    completed_since_review = (
        config.features.initial_completed_sessions_since_review
        if prior_state is None
        else prior_state.completed_sessions_since_review
    )
    instruments = tuple(
        _instrument(
            symbol=symbol,
            series=market_inputs.series[symbol],
            qqq_series=market_inputs.series["QQQ"],
            current_weight=current_weights[symbol],
            parent_decision=parent_decision,
            completed_sessions_since_review=completed_since_review,
            config=config,
        )
        for symbol in config.reference_universe
    )
    request_id = stable_id(
        "candidate-prospective-request",
        artifact.challenger_id,
        artifact.bundle_hash,
        parent_decision.portfolio_decision_id,
        parent_decision.decision_hash,
        source_manifest.manifest_hash,
        config_bundle.manifest_hash,
    )
    request = build_candidate_decision_request(
        request_id=request_id,
        challenger_id=artifact.challenger_id,
        candidate_artifact_hash=artifact.bundle_hash,
        strategy_id=config.strategy_id,
        strategy_version=config.strategy_version,
        decision_time=parent_decision.decision_created_at,
        signal_data_cutoff=parent_decision.decision_created_at,
        variant=CandidateEvaluationVariantV1(),
        instruments=instruments,
        constraints=config.constraints,
        strategy_parameters=config_bundle.strategy_parameters,
        source_data_manifest_hash=source_manifest.manifest_hash,
    )
    payload = {
        "schema_version": "candidate_prospective_request_evidence_v1",
        "prospective_request_id": request.request_id,
        "challenger_id": artifact.challenger_id,
        "candidate_artifact_bundle_id": artifact.bundle_id,
        "candidate_artifact_hash": artifact.bundle_hash,
        "candidate_config_hash": artifact.config_hash,
        "strategy_config_content_sha256": config.strategy_config_content_sha256,
        "parent_run_id": parent_decision.run_id,
        "parent_portfolio_decision_id": parent_decision.portfolio_decision_id,
        "parent_scheduled_at": parent_decision.scheduled_at,
        "calendar_session_id": parent_decision.input_manifest.calendar_session_id,
        "evaluation_anchor_id": evaluation_anchor.evaluation_anchor_id,
        "prior_prospective_request_id": source_manifest.prior_prospective_request_id,
        "source_manifest": source_manifest,
        "request": request,
        "created_at": request.decision_time,
        "real_order_routing": False,
        "automatic_promotion_enabled": False,
        "challenger_lifecycle_advance_enabled": False,
        "shadow_activation_enabled": False,
    }
    return ProspectiveRequestEvidenceV1.model_validate(
        {**payload, "evidence_hash": canonical_hash(payload)}
    )


def build_successful_execution_evidence(
    *,
    request_evidence: ProspectiveRequestEvidenceV1,
    attestation: CandidateRuntimeAttestationV1,
    security: CandidateExecutionSecurityV1,
    primary_response: CandidateDecisionResponseV1,
    replay_response: CandidateDecisionResponseV1,
) -> ProspectiveExecutionEvidenceV1:
    primary_response.assert_bound_to(request_evidence.request)
    replay_response.assert_bound_to(request_evidence.request)
    if primary_response.output_hash != replay_response.output_hash:
        raise ProspectiveCandidateError("COMMANDER_CANDIDATE_NONDETERMINISTIC")
    runtime_hash = canonical_hash(attestation)
    payload = {
        "schema_version": "candidate_prospective_execution_evidence_v1",
        "execution_id": stable_id(
            "candidate-prospective-execution",
            request_evidence.request.request_hash,
            primary_response.output_hash,
            runtime_hash,
            security.security_contract_hash,
        ),
        "prospective_request_id": request_evidence.prospective_request_id,
        "challenger_id": request_evidence.challenger_id,
        "candidate_artifact_hash": request_evidence.candidate_artifact_hash,
        "request_hash": request_evidence.request.request_hash,
        "status": ProspectiveExecutionStatus.SUCCEEDED,
        "runtime_attestation_hash": runtime_hash,
        "security_contract_hash": security.security_contract_hash,
        "primary_response": primary_response,
        "replay_response": replay_response,
        "deterministic_match": True,
        "error_code": None,
        "created_at": request_evidence.request.decision_time,
        "real_order_routing": False,
        "evidence_recorded": True,
        "challenger_status_advanced": False,
        "shadow_started": False,
    }
    return ProspectiveExecutionEvidenceV1.model_validate(
        {**payload, "execution_hash": canonical_hash(payload)}
    )


def build_failed_execution_evidence(
    *,
    request_evidence: ProspectiveRequestEvidenceV1,
    attestation: CandidateRuntimeAttestationV1,
    security: CandidateExecutionSecurityV1,
    error_code: str,
) -> ProspectiveExecutionEvidenceV1:
    runtime_hash = canonical_hash(attestation)
    payload = {
        "schema_version": "candidate_prospective_execution_evidence_v1",
        "execution_id": stable_id(
            "candidate-prospective-execution-failure",
            request_evidence.request.request_hash,
            runtime_hash,
            security.security_contract_hash,
            error_code,
        ),
        "prospective_request_id": request_evidence.prospective_request_id,
        "challenger_id": request_evidence.challenger_id,
        "candidate_artifact_hash": request_evidence.candidate_artifact_hash,
        "request_hash": request_evidence.request.request_hash,
        "status": ProspectiveExecutionStatus.FAILED,
        "runtime_attestation_hash": runtime_hash,
        "security_contract_hash": security.security_contract_hash,
        "primary_response": None,
        "replay_response": None,
        "deterministic_match": False,
        "error_code": error_code,
        "created_at": request_evidence.request.decision_time,
        "real_order_routing": False,
        "evidence_recorded": True,
        "challenger_status_advanced": False,
        "shadow_started": False,
    }
    return ProspectiveExecutionEvidenceV1.model_validate(
        {**payload, "execution_hash": canonical_hash(payload)}
    )


def _instrument(
    *,
    symbol: str,
    series: CompletedDailySeries,
    qqq_series: CompletedDailySeries,
    current_weight: float,
    parent_decision: Q1StrategyDecision,
    completed_sessions_since_review: int,
    config: ProspectiveCandidateConfigV1,
) -> CandidateInstrumentInputV1:
    features = config.features
    candidate_features = list(
        build_candidate_price_features(
            series=series,
            qqq_series=qqq_series,
            short_return_sessions=features.total_return_short_sessions,
            long_return_sessions=features.total_return_long_sessions,
            moving_average_sessions=features.moving_average_sessions,
            realized_volatility_sessions=(
                features.realized_volatility_sessions
            ),
            downside_beta_sessions=features.downside_beta_sessions,
            annualization_sessions=features.annualization_sessions,
            realized_volatility_ddof=features.realized_volatility_ddof,
            minimum_variance=features.minimum_variance,
            source_revision=features.source_revision,
            formula_version=config.producer_version,
        )
    )
    latest_indexes = range(
        len(series.session_dates) - 1,
        len(series.session_dates),
    )
    candidate_features.append(
        _bar_feature(
            name="completed_sessions_since_review",
            value=float(completed_sessions_since_review),
            series=series,
            indexes=latest_indexes,
            source_revision=features.source_revision,
            formula_version=config.producer_version,
            extra_source={
                "calendar_session_id": (
                    parent_decision.input_manifest.calendar_session_id
                ),
            },
        )
    )
    parent_target = float(
        parent_decision.target_weights.get(symbol, 0)
        if symbol in {"QQQ", "SOXX"}
        else 0
    )
    candidate_features.append(
        CandidateFeatureValueV1(
            name="parent_target_weight",
            value=parent_target,
            source_event_time=parent_decision.scheduled_at,
            available_at=parent_decision.decision_created_at,
            source_revision=features.source_revision,
            revision_available_at=parent_decision.decision_created_at,
            revision_was_known_at_cutoff=True,
            source_hash=canonical_hash(
                {
                    "formula_version": config.producer_version,
                    "name": "parent_target_weight",
                    "symbol": symbol,
                    "value": parent_target,
                    "parent_portfolio_decision_id": (
                        parent_decision.portfolio_decision_id
                    ),
                    "parent_decision_hash": parent_decision.decision_hash,
                }
            ),
        )
    )
    return CandidateInstrumentInputV1(
        symbol=symbol,
        current_weight=current_weight,
        membership_available_at=config.membership.available_at,
        membership_valid_from=config.membership.valid_from,
        membership_valid_until=config.membership.valid_until,
        instrument_is_non_survivor=config.membership.instrument_is_non_survivor,
        features=tuple(sorted(candidate_features, key=lambda item: item.name)),
    )


def build_candidate_price_features(
    *,
    series: CompletedDailySeries,
    qqq_series: CompletedDailySeries,
    short_return_sessions: int,
    long_return_sessions: int,
    moving_average_sessions: int,
    realized_volatility_sessions: int,
    downside_beta_sessions: int,
    annualization_sessions: int,
    realized_volatility_ddof: int,
    minimum_variance: float,
    source_revision: int,
    formula_version: str,
) -> tuple[CandidateFeatureValueV1, ...]:
    """Build source-bound PIT price features for a host-owned variant."""

    if short_return_sessions >= long_return_sessions:
        raise ProspectiveCandidateError(
            "PROSPECTIVE_RETURN_WINDOWS_INVALID"
        )
    downside_beta, downside_count = _downside_beta(
        series,
        qqq_series,
        sessions=downside_beta_sessions,
        minimum_variance=minimum_variance,
    )
    price_features = {
        f"total_return_{short_return_sessions}": _total_return(
            series,
            short_return_sessions,
        ),
        f"total_return_{long_return_sessions}": _total_return(
            series,
            long_return_sessions,
        ),
        f"moving_average_gap_{moving_average_sessions}": (
            _moving_average_gap(series, moving_average_sessions)
        ),
        f"realized_volatility_{realized_volatility_sessions}": (
            _realized_volatility(
                series,
                sessions=realized_volatility_sessions,
                annualization_sessions=annualization_sessions,
                ddof=realized_volatility_ddof,
            )
        ),
        f"downside_beta_{downside_beta_sessions}_qqq": downside_beta,
        f"downside_observation_count_{downside_beta_sessions}": float(
            downside_count
        ),
    }
    built: list[CandidateFeatureValueV1] = []
    for name, value in sorted(price_features.items()):
        if name.startswith("total_return_"):
            window = int(name.rsplit("_", 1)[1]) + 1
        elif name.startswith("moving_average_gap_"):
            window = moving_average_sessions
        elif name.startswith("realized_volatility_"):
            window = realized_volatility_sessions + 1
        elif name.startswith("downside_"):
            window = downside_beta_sessions + 1
        else:
            raise ProspectiveCandidateError(
                "PROSPECTIVE_FEATURE_WINDOW_UNKNOWN"
            )
        indexes = range(
            len(series.session_dates) - window,
            len(series.session_dates),
        )
        built.append(
            _bar_feature(
                name=name,
                value=value,
                series=series,
                indexes=indexes,
                source_revision=source_revision,
                formula_version=formula_version,
                additional_series=(
                    (qqq_series,)
                    if name.startswith("downside_")
                    and qqq_series.symbol != series.symbol
                    else ()
                ),
            )
        )
    return tuple(sorted(built, key=lambda item: item.name))


def _total_return(series: CompletedDailySeries, sessions: int) -> float:
    _require_history(series, sessions + 1)
    return float(series.adjusted_closes[-1] / series.adjusted_closes[-sessions - 1] - 1)


def _moving_average_gap(series: CompletedDailySeries, sessions: int) -> float:
    _require_history(series, sessions)
    values = series.adjusted_closes[-sessions:]
    average = sum(values, start=Decimal("0")) / Decimal(sessions)
    if average <= 0:
        raise ProspectiveCandidateError("PROSPECTIVE_MOVING_AVERAGE_INVALID")
    return float(series.adjusted_closes[-1] / average - 1)


def _realized_volatility(
    series: CompletedDailySeries,
    *,
    sessions: int,
    annualization_sessions: int,
    ddof: int,
) -> float:
    returns = _log_returns(series, sessions)
    denominator = len(returns) - ddof
    if denominator <= 0:
        raise ProspectiveCandidateError("PROSPECTIVE_VOLATILITY_SAMPLE_INVALID")
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / denominator
    return math.sqrt(max(0.0, variance) * annualization_sessions)


def _downside_beta(
    series: CompletedDailySeries,
    qqq_series: CompletedDailySeries,
    *,
    sessions: int,
    minimum_variance: float,
) -> tuple[float, int]:
    if series.session_dates != qqq_series.session_dates:
        raise ProspectiveCandidateError("PROSPECTIVE_DOWNSIDE_SERIES_MISALIGNED")
    asset_returns = _log_returns(series, sessions)
    qqq_returns = _log_returns(qqq_series, sessions)
    pairs = [
        (asset, market)
        for asset, market in zip(asset_returns, qqq_returns, strict=True)
        if market < 0.0
    ]
    if len(pairs) < 2:
        raise ProspectiveCandidateError("PROSPECTIVE_DOWNSIDE_SAMPLE_INSUFFICIENT")
    asset_mean = sum(item[0] for item in pairs) / len(pairs)
    market_mean = sum(item[1] for item in pairs) / len(pairs)
    covariance_numerator = sum(
        (asset - asset_mean) * (market - market_mean)
        for asset, market in pairs
    )
    variance_numerator = sum(
        (market - market_mean) ** 2 for _, market in pairs
    )
    if variance_numerator / (len(pairs) - 1) <= minimum_variance:
        raise ProspectiveCandidateError("PROSPECTIVE_DOWNSIDE_VARIANCE_INVALID")
    return covariance_numerator / variance_numerator, len(pairs)


def _log_returns(series: CompletedDailySeries, sessions: int) -> tuple[float, ...]:
    _require_history(series, sessions + 1)
    closes = series.adjusted_closes[-sessions - 1 :]
    return tuple(
        math.log(float(current / previous))
        for previous, current in pairwise(closes)
    )


def _bar_feature(
    *,
    name: str,
    value: float,
    series: CompletedDailySeries,
    indexes: range,
    source_revision: int,
    formula_version: str,
    extra_source: dict[str, object] | None = None,
    additional_series: tuple[CompletedDailySeries, ...] = (),
) -> CandidateFeatureValueV1:
    selected = tuple(indexes)
    if not selected:
        raise ProspectiveCandidateError("PROSPECTIVE_FEATURE_SOURCE_EMPTY")
    source_series = tuple(
        sorted(
            {item.symbol: item for item in (series, *additional_series)}.values(),
            key=lambda item: item.symbol,
        )
    )
    if any(item.session_dates != series.session_dates for item in source_series):
        raise ProspectiveCandidateError("PROSPECTIVE_FEATURE_SERIES_MISALIGNED")
    event_time = max(
        item.event_times[index]
        for item in source_series
        for index in selected
    )
    available_at = max(
        item.available_ats[index]
        for item in source_series
        for index in selected
    )
    source = {
        "formula_version": formula_version,
        "name": name,
        "symbol": series.symbol,
        "value": value,
        "source_bars": [
            {
                "symbol": item.symbol,
                "bar_id": item.bar_ids[index],
                "payload_hash": item.payload_hashes[index],
            }
            for item in source_series
            for index in selected
        ],
        **(extra_source or {}),
    }
    return CandidateFeatureValueV1(
        name=name,
        value=value,
        source_event_time=event_time,
        available_at=available_at,
        source_revision=source_revision,
        revision_available_at=available_at,
        revision_was_known_at_cutoff=True,
        source_hash=canonical_hash(source),
    )


def _source_bars(inputs: AlignedDailyInputs) -> tuple[ProspectiveSourceBarV1, ...]:
    rows: list[ProspectiveSourceBarV1] = []
    for symbol, series in sorted(inputs.series.items()):
        for index, session_date in enumerate(series.session_dates):
            rows.append(
                ProspectiveSourceBarV1(
                    bar_id=series.bar_ids[index],
                    symbol=symbol,
                    session_date=session_date,
                    source_event_time=series.event_times[index],
                    available_at=series.available_ats[index],
                    payload_hash=series.payload_hashes[index],
                )
            )
    return tuple(
        sorted(rows, key=lambda item: (item.symbol, item.session_date, item.bar_id))
    )


def _validate_parameter_alignment(
    config: ProspectiveCandidateConfigV1,
    parameters: Mapping[str, object],
) -> None:
    expected = {
        "short_return_sessions": config.features.total_return_short_sessions,
        "long_return_sessions": config.features.total_return_long_sessions,
        "moving_average_sessions": config.features.moving_average_sessions,
        "downside_beta_sessions": config.features.downside_beta_sessions,
    }
    for name, value in expected.items():
        if parameters.get(name) != value:
            raise ValueError(f"prospective feature window differs from {name}")
    review_interval = parameters.get("review_interval_sessions")
    if (
        isinstance(review_interval, bool)
        or not isinstance(review_interval, int)
        or config.features.initial_completed_sessions_since_review
        < review_interval
    ):
        raise ValueError("prospective initial review clock is below strategy interval")


def _require_history(series: CompletedDailySeries, required: int) -> None:
    if len(series.session_dates) < required:
        raise ProspectiveCandidateError("PROSPECTIVE_HISTORY_INSUFFICIENT")


def _valid_symbol(value: str) -> bool:
    import re

    return re.fullmatch(SYMBOL_PATTERN, value) is not None
