from __future__ import annotations

import math
from datetime import datetime
from typing import Literal, Protocol, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.research.contracts import (
    HASH_PATTERN,
    IDENTIFIER_PATTERN,
    SYMBOL_PATTERN,
    VERSION_PATTERN,
)
from trading.research.evaluation_contracts import BASE_VARIANT_ID


class CandidateFeatureValueV1(DomainModel):
    """One point-in-time feature value exposed to an isolated Challenger."""

    name: str = Field(pattern=IDENTIFIER_PATTERN)
    value: float
    source_event_time: datetime
    available_at: datetime
    source_revision: int = Field(ge=0)
    revision_available_at: datetime
    revision_was_known_at_cutoff: bool
    source_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "source_event_time",
        "available_at",
        "revision_available_at",
        mode="after",
    )
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("value", mode="after")
    @classmethod
    def validate_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("candidate feature value must be finite")
        return value


class CandidateInstrumentInputV1(DomainModel):
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    current_weight: float = Field(ge=0, le=1)
    membership_available_at: datetime
    membership_valid_from: datetime
    membership_valid_until: datetime | None = None
    instrument_is_non_survivor: bool
    features: tuple[CandidateFeatureValueV1, ...] = Field(min_length=1)

    @field_validator(
        "membership_available_at",
        "membership_valid_from",
        "membership_valid_until",
        mode="after",
    )
    @classmethod
    def validate_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware_utc(value)

    @field_validator("features", mode="after")
    @classmethod
    def validate_features(
        cls,
        value: tuple[CandidateFeatureValueV1, ...],
    ) -> tuple[CandidateFeatureValueV1, ...]:
        names = tuple(item.name for item in value)
        if names != tuple(sorted(set(names))):
            raise ValueError("candidate features must be unique and sorted")
        return value


class CandidateDecisionConstraintsV1(DomainModel):
    """Host-owned portfolio boundary; candidates cannot relax it."""

    long_only: Literal[True] = True
    leverage_permitted: Literal[False] = False
    new_symbols_permitted: Literal[False] = False
    maximum_gross_weight: float = Field(gt=0, le=1)
    minimum_cash_weight: float = Field(ge=0, lt=1)
    maximum_weight_by_symbol: dict[str, float] = Field(min_length=1)
    numeric_tolerance: float = Field(gt=0)

    @field_validator("maximum_weight_by_symbol", mode="after")
    @classmethod
    def validate_symbol_caps(cls, value: dict[str, float]) -> dict[str, float]:
        if list(value) != sorted(value):
            raise ValueError("candidate symbol caps must be sorted")
        for symbol, cap in value.items():
            if (
                not math.isfinite(cap)
                or cap < 0
                or cap > 1
                or not _valid_symbol(symbol)
            ):
                raise ValueError("candidate symbol cap is invalid")
        return value

    @field_validator(
        "maximum_gross_weight",
        "minimum_cash_weight",
        "numeric_tolerance",
        mode="after",
    )
    @classmethod
    def validate_finite_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("candidate constraint must be finite")
        return value

    @model_validator(mode="after")
    def validate_cash_boundary(self) -> Self:
        if (
            self.maximum_gross_weight
            > 1 - self.minimum_cash_weight + self.numeric_tolerance
        ):
            raise ValueError("gross exposure conflicts with minimum cash")
        return self


class CandidateEvaluationVariantV1(DomainModel):
    """Exactly one isolated host-owned falsification transformation."""

    parameter_neighborhood_id: str = Field(
        default=BASE_VARIANT_ID,
        pattern=IDENTIFIER_PATTERN,
    )
    data_ablation_id: str = Field(
        default=BASE_VARIANT_ID,
        pattern=IDENTIFIER_PATTERN,
    )
    date_shift_id: str = Field(
        default=BASE_VARIANT_ID,
        pattern=IDENTIFIER_PATTERN,
    )
    inversion_id: str = Field(
        default=BASE_VARIANT_ID,
        pattern=IDENTIFIER_PATTERN,
    )
    shuffle_id: str = Field(
        default=BASE_VARIANT_ID,
        pattern=IDENTIFIER_PATTERN,
    )

    @model_validator(mode="after")
    def validate_isolated_variant(self) -> Self:
        changed = sum(
            value != BASE_VARIANT_ID
            for value in (
                self.parameter_neighborhood_id,
                self.data_ablation_id,
                self.date_shift_id,
                self.inversion_id,
                self.shuffle_id,
            )
        )
        if changed > 1:
            raise ValueError("evaluation variants must isolate one transformation")
        return self

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.parameter_neighborhood_id,
            self.data_ablation_id,
            self.date_shift_id,
            self.inversion_id,
            self.shuffle_id,
        )


class CandidateDecisionRequestV1(DomainModel):
    """The only data contract an isolated candidate may receive."""

    schema_version: str = Field(default="candidate_decision_request_v1")
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    strategy_version: str = Field(pattern=VERSION_PATTERN)
    decision_time: datetime
    signal_data_cutoff: datetime
    variant: CandidateEvaluationVariantV1
    instruments: tuple[CandidateInstrumentInputV1, ...] = Field(min_length=1)
    constraints: CandidateDecisionConstraintsV1
    strategy_parameters: dict[str, JsonValue]
    strategy_parameters_hash: str = Field(pattern=HASH_PATTERN)
    source_data_manifest_hash: str = Field(pattern=HASH_PATTERN)
    request_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("decision_time", "signal_data_cutoff", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("instruments", mode="after")
    @classmethod
    def validate_instruments(
        cls,
        value: tuple[CandidateInstrumentInputV1, ...],
    ) -> tuple[CandidateInstrumentInputV1, ...]:
        symbols = tuple(item.symbol for item in value)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("candidate instruments must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.signal_data_cutoff > self.decision_time:
            raise ValueError("signal_data_cutoff cannot exceed decision_time")
        if canonical_hash(self.strategy_parameters) != self.strategy_parameters_hash:
            raise ValueError("strategy parameter hash mismatch")
        symbols = tuple(item.symbol for item in self.instruments)
        if symbols != tuple(self.constraints.maximum_weight_by_symbol):
            raise ValueError("candidate universe differs from host constraints")
        if (
            sum(item.current_weight for item in self.instruments)
            > self.constraints.maximum_gross_weight
            + self.constraints.numeric_tolerance
        ):
            raise ValueError("current risky weights violate host gross boundary")
        for instrument in self.instruments:
            if instrument.membership_available_at > self.signal_data_cutoff:
                raise ValueError("future constituent membership entered candidate input")
            if instrument.membership_valid_from > self.decision_time:
                raise ValueError("instrument membership is not yet effective")
            if (
                instrument.membership_valid_until is not None
                and instrument.membership_valid_until < self.decision_time
            ):
                raise ValueError("instrument membership has expired")
            if (
                instrument.instrument_is_non_survivor
                and instrument.membership_valid_until is None
            ):
                raise ValueError("non-survivor input requires membership_valid_until")
            for feature in instrument.features:
                if (
                    feature.available_at > self.signal_data_cutoff
                    or feature.source_event_time > self.signal_data_cutoff
                    or feature.revision_available_at > self.signal_data_cutoff
                    or not feature.revision_was_known_at_cutoff
                ):
                    raise ValueError("future or revised data entered candidate input")
                if feature.source_event_time > feature.available_at:
                    raise ValueError("feature source event follows availability")
        payload = self.model_dump(mode="python", exclude={"request_hash"})
        if canonical_hash(payload) != self.request_hash:
            raise ValueError("candidate request hash mismatch")
        return self


class CandidateTargetV1(DomainModel):
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    score: float
    target_weight: float = Field(ge=0, le=1)

    @field_validator("score", "target_weight", mode="after")
    @classmethod
    def validate_finite_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("candidate target values must be finite")
        return value


class CandidateDecisionResponseV1(DomainModel):
    """Candidate output. It contains no fills, orders, returns, or PnL."""

    schema_version: str = Field(default="candidate_decision_response_v1")
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    request_hash: str = Field(pattern=HASH_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    targets: tuple[CandidateTargetV1, ...] = Field(min_length=1)
    diagnostics: dict[str, JsonValue] = Field(default_factory=dict)
    output_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("targets", mode="after")
    @classmethod
    def validate_targets(
        cls,
        value: tuple[CandidateTargetV1, ...],
    ) -> tuple[CandidateTargetV1, ...]:
        symbols = tuple(item.symbol for item in value)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("candidate targets must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"output_hash"})
        if canonical_hash(payload) != self.output_hash:
            raise ValueError("candidate response hash mismatch")
        return self

    def assert_bound_to(self, request: CandidateDecisionRequestV1) -> None:
        if (
            self.request_id != request.request_id
            or self.request_hash != request.request_hash
            or self.challenger_id != request.challenger_id
            or self.candidate_artifact_hash != request.candidate_artifact_hash
        ):
            raise ValueError("candidate response binding mismatch")
        expected_symbols = tuple(item.symbol for item in request.instruments)
        actual_symbols = tuple(item.symbol for item in self.targets)
        if actual_symbols != expected_symbols:
            raise ValueError("candidate response introduced or omitted a symbol")
        constraints = request.constraints
        gross = 0.0
        for target in self.targets:
            cap = constraints.maximum_weight_by_symbol[target.symbol]
            if target.target_weight > cap + constraints.numeric_tolerance:
                raise ValueError("candidate target exceeds a host-owned symbol cap")
            gross += target.target_weight
        if gross > constraints.maximum_gross_weight + constraints.numeric_tolerance:
            raise ValueError("candidate response exceeds maximum gross exposure")
        if 1 - gross < constraints.minimum_cash_weight - constraints.numeric_tolerance:
            raise ValueError("candidate response violates minimum cash")


class CandidateExecutor(Protocol):
    def execute(
        self,
        request: CandidateDecisionRequestV1,
    ) -> CandidateDecisionResponseV1: ...


def build_candidate_decision_request(
    *,
    request_id: str,
    challenger_id: str,
    candidate_artifact_hash: str,
    strategy_id: str,
    strategy_version: str,
    decision_time: datetime,
    signal_data_cutoff: datetime,
    variant: CandidateEvaluationVariantV1,
    instruments: tuple[CandidateInstrumentInputV1, ...],
    constraints: CandidateDecisionConstraintsV1,
    strategy_parameters: dict[str, JsonValue],
    source_data_manifest_hash: str,
) -> CandidateDecisionRequestV1:
    ordered = tuple(sorted(instruments, key=lambda item: item.symbol))
    payload = {
        "schema_version": "candidate_decision_request_v1",
        "request_id": request_id,
        "challenger_id": challenger_id,
        "candidate_artifact_hash": candidate_artifact_hash,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "decision_time": require_aware_utc(decision_time),
        "signal_data_cutoff": require_aware_utc(signal_data_cutoff),
        "variant": variant,
        "instruments": ordered,
        "constraints": constraints,
        "strategy_parameters": strategy_parameters,
        "strategy_parameters_hash": canonical_hash(strategy_parameters),
        "source_data_manifest_hash": source_data_manifest_hash,
    }
    return CandidateDecisionRequestV1.model_validate(
        {**payload, "request_hash": canonical_hash(payload)}
    )


def build_candidate_decision_response(
    *,
    request: CandidateDecisionRequestV1,
    targets: tuple[CandidateTargetV1, ...],
    diagnostics: dict[str, JsonValue] | None = None,
) -> CandidateDecisionResponseV1:
    ordered = tuple(sorted(targets, key=lambda item: item.symbol))
    payload = {
        "schema_version": "candidate_decision_response_v1",
        "request_id": request.request_id,
        "request_hash": request.request_hash,
        "challenger_id": request.challenger_id,
        "candidate_artifact_hash": request.candidate_artifact_hash,
        "targets": ordered,
        "diagnostics": diagnostics or {},
    }
    response = CandidateDecisionResponseV1.model_validate(
        {**payload, "output_hash": canonical_hash(payload)}
    )
    response.assert_bound_to(request)
    return response


def _valid_symbol(value: str) -> bool:
    import re

    return re.fullmatch(SYMBOL_PATTERN, value) is not None
