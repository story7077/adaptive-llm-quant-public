from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

import yaml
from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.research.contracts import (
    HASH_PATTERN,
    IDENTIFIER_PATTERN,
    SYMBOL_PATTERN,
    VERSION_PATTERN,
)
from trading.research.evaluation_contracts import KnownFactorReturnV1
from trading.research.prospective import (
    ProspectiveExecutionEvidenceV1,
    ProspectiveRequestEvidenceV1,
)

PROSPECTIVE_OUTCOME_CONFIG_FILE = (
    "research/candidate-prospective-outcomes.yaml"
)


class ProspectiveOutcomeError(RuntimeError):
    """Fail-closed forward-outcome collection error."""


class ProspectiveOutcomeImplementationConfigV1(DomainModel):
    price_rule: Literal["NEXT_SESSION_ADJUSTED_CLOSE"]
    delay_sessions: Literal[1]
    return_horizon_sessions: Literal[1]
    outcome_data_delay_minutes: int = Field(gt=0, le=1_440)


class ProspectiveKnownFactorConfigV1(DomainModel):
    factor_id: str = Field(pattern=IDENTIFIER_PATTERN)
    long_symbol: str = Field(pattern=SYMBOL_PATTERN)
    short_symbol: str = Field(pattern=SYMBOL_PATTERN)

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        if self.long_symbol == self.short_symbol:
            raise ValueError("prospective factor pair must use two symbols")
        return self


class ProspectiveMarketContextConfigV1(DomainModel):
    market_symbol: str = Field(pattern=SYMBOL_PATTERN)
    sector_symbol: str = Field(pattern=SYMBOL_PATTERN)
    known_factors: tuple[ProspectiveKnownFactorConfigV1, ...] = Field(
        min_length=1
    )
    up_regime_return_threshold: float
    down_regime_return_threshold: float

    @field_validator(
        "up_regime_return_threshold",
        "down_regime_return_threshold",
        mode="after",
    )
    @classmethod
    def validate_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("prospective regime threshold must be finite")
        return value

    @field_validator("known_factors", mode="after")
    @classmethod
    def validate_factors(
        cls,
        value: tuple[ProspectiveKnownFactorConfigV1, ...],
    ) -> tuple[ProspectiveKnownFactorConfigV1, ...]:
        ids = tuple(item.factor_id for item in value)
        if ids != tuple(sorted(set(ids))):
            raise ValueError(
                "prospective factor IDs must be unique and sorted"
            )
        return value

    @model_validator(mode="after")
    def validate_regime_thresholds(self) -> Self:
        if (
            self.down_regime_return_threshold >= 0
            or self.up_regime_return_threshold <= 0
            or self.down_regime_return_threshold
            >= self.up_regime_return_threshold
        ):
            raise ValueError("prospective regime thresholds are invalid")
        return self


class ProspectiveCapacityConfigV1(DomainModel):
    adv_lookback_completed_sessions: int = Field(gt=1)


class ProspectiveCostModelReferenceV1(DomainModel):
    config_path: str = Field(min_length=1, max_length=240)
    config_content_sha256: str = Field(pattern=HASH_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)

    @field_validator("config_path", mode="after")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                "prospective outcome cost config must stay inside config"
            )
        return value


class ProspectiveReadinessConfigV1(DomainModel):
    minimum_common_sessions: int = Field(gt=1)
    minimum_observations: int = Field(gt=1)
    numeric_tolerance: float = Field(gt=0)

    @field_validator("numeric_tolerance", mode="after")
    @classmethod
    def validate_tolerance(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError(
                "prospective outcome numeric tolerance must be finite"
            )
        return value


class ProspectiveOutcomeOperationsConfigV1(DomainModel):
    poll_seconds: int = Field(gt=0, le=3600)
    history_refresh_cooldown_seconds: int = Field(gt=0, le=86_400)
    outcome_bar_query_limit: int = Field(gt=1)


class ProspectiveOutcomeSafetyConfigV1(DomainModel):
    real_order_routing: Literal[False] = False
    automatic_promotion_enabled: Literal[False] = False
    challenger_lifecycle_advance_enabled: Literal[False] = False
    shadow_activation_enabled: Literal[False] = False
    broker_access_permitted: Literal[False] = False


class ProspectiveOutcomeConfigV1(DomainModel):
    schema_version: Literal["candidate_prospective_outcome_config_v1"]
    producer_version: str = Field(pattern=VERSION_PATTERN)
    calendar_version: str = Field(pattern=VERSION_PATTERN)
    market_dataset_version: str = Field(min_length=1, max_length=120)
    timeframe: Literal["1Day"]
    adjustment: Literal["all"]
    implementation: ProspectiveOutcomeImplementationConfigV1
    market_context: ProspectiveMarketContextConfigV1
    capacity: ProspectiveCapacityConfigV1
    cost_model: ProspectiveCostModelReferenceV1
    readiness: ProspectiveReadinessConfigV1
    operations: ProspectiveOutcomeOperationsConfigV1
    safety: ProspectiveOutcomeSafetyConfigV1


@dataclass(frozen=True, slots=True)
class ProspectiveOutcomeConfigBundle:
    config: ProspectiveOutcomeConfigV1
    commission_bps: float
    spread_bps: float
    delay_bps: float
    cost_model_hash: str
    manifest_hash: str
    path: Path
    cost_path: Path


def prospective_outcome_market_symbols(
    config: ProspectiveOutcomeConfigBundle,
    *,
    candidate_symbols: tuple[str, ...],
) -> tuple[str, ...]:
    symbols = set(candidate_symbols)
    context = config.config.market_context
    symbols.update((context.market_symbol, context.sector_symbol))
    for factor in context.known_factors:
        symbols.update((factor.long_symbol, factor.short_symbol))
    return tuple(sorted(symbols))


class ProspectiveOutcomeSourceBarV1(DomainModel):
    bar_id: str = Field(pattern=IDENTIFIER_PATTERN)
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    session_date: date
    source_event_time: datetime
    available_at: datetime
    adjusted_close: float = Field(gt=0)
    volume: float = Field(ge=0)
    payload_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("source_event_time", "available_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("adjusted_close", "volume", mode="after")
    @classmethod
    def validate_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("prospective outcome bar value must be finite")
        return value

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.source_event_time > self.available_at:
            raise ValueError(
                "prospective outcome bar is available before its event"
            )
        return self


class ProspectiveOutcomeEvidenceV1(DomainModel):
    schema_version: Literal["candidate_prospective_outcome_evidence_v1"]
    outcome_id: str = Field(pattern=IDENTIFIER_PATTERN)
    prospective_request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    execution_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    request_hash: str = Field(pattern=HASH_PATTERN)
    execution_hash: str = Field(pattern=HASH_PATTERN)
    decision_calendar_session_id: str = Field(pattern=IDENTIFIER_PATTERN)
    implementation_calendar_session_id: str = Field(
        pattern=IDENTIFIER_PATTERN
    )
    evaluation_calendar_session_id: str = Field(
        pattern=IDENTIFIER_PATTERN
    )
    calendar_version: str = Field(pattern=VERSION_PATTERN)
    market_dataset_version: str = Field(min_length=1, max_length=120)
    timeframe: Literal["1Day"]
    adjustment: Literal["all"]
    decision_time: datetime
    implementation_close_at: datetime
    evaluation_close_at: datetime
    outcome_data_cutoff: datetime
    outcome_available_at: datetime
    evaluation_nav_usd: float = Field(gt=0)
    candidate_current_weights: dict[str, float]
    candidate_target_weights: dict[str, float]
    baseline_current_weights: dict[str, float]
    baseline_target_weights: dict[str, float]
    forward_returns: dict[str, float]
    adv_usd: dict[str, float]
    adv_lookback_completed_sessions: int = Field(gt=1)
    market_symbol: str = Field(pattern=SYMBOL_PATTERN)
    sector_symbol: str = Field(pattern=SYMBOL_PATTERN)
    known_factor_sources: tuple[ProspectiveKnownFactorConfigV1, ...] = Field(
        min_length=1
    )
    market_return: float
    sector_return: float
    known_factor_returns: tuple[KnownFactorReturnV1, ...] = Field(
        min_length=1
    )
    regime: Literal["UP", "DOWN", "RANGE"]
    commission_bps: float = Field(ge=0)
    spread_bps: float = Field(ge=0)
    delay_bps: float = Field(ge=0)
    source_bars: tuple[ProspectiveOutcomeSourceBarV1, ...] = Field(
        min_length=2
    )
    source_manifest_hash: str = Field(pattern=HASH_PATTERN)
    config_manifest_hash: str = Field(pattern=HASH_PATTERN)
    cost_model_hash: str = Field(pattern=HASH_PATTERN)
    numeric_tolerance: float = Field(gt=0)
    created_at: datetime
    real_order_routing: Literal[False] = False
    automatic_promotion_enabled: Literal[False] = False
    challenger_lifecycle_advance_enabled: Literal[False] = False
    shadow_activation_enabled: Literal[False] = False
    broker_access_permitted: Literal[False] = False
    outcome_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "decision_time",
        "implementation_close_at",
        "evaluation_close_at",
        "outcome_data_cutoff",
        "outcome_available_at",
        "created_at",
        mode="after",
    )
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator(
        "evaluation_nav_usd",
        "market_return",
        "sector_return",
        "commission_bps",
        "spread_bps",
        "delay_bps",
        "numeric_tolerance",
        mode="after",
    )
    @classmethod
    def validate_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError(
                "prospective outcome numeric value must be finite"
            )
        return value

    @field_validator("known_factor_returns", mode="after")
    @classmethod
    def validate_known_factors(
        cls,
        value: tuple[KnownFactorReturnV1, ...],
    ) -> tuple[KnownFactorReturnV1, ...]:
        ids = tuple(item.factor_id for item in value)
        if ids != tuple(sorted(set(ids))):
            raise ValueError(
                "prospective known factors must be unique and sorted"
            )
        return value

    @field_validator("source_bars", mode="after")
    @classmethod
    def validate_source_bars(
        cls,
        value: tuple[ProspectiveOutcomeSourceBarV1, ...],
    ) -> tuple[ProspectiveOutcomeSourceBarV1, ...]:
        keys = tuple(
            (item.session_date, item.symbol, item.bar_id) for item in value
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError(
                "prospective outcome source bars must be unique and sorted"
            )
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if not (
            self.decision_time
            < self.implementation_close_at
            < self.evaluation_close_at
            < self.outcome_available_at
            <= self.outcome_data_cutoff
            <= self.created_at
        ):
            raise ValueError(
                "prospective outcome timestamps are not chronological"
            )
        universes = (
            set(self.candidate_current_weights),
            set(self.candidate_target_weights),
            set(self.baseline_current_weights),
            set(self.baseline_target_weights),
            set(self.forward_returns),
            set(self.adv_usd),
        )
        if not universes[0] or any(
            universe != universes[0] for universe in universes[1:]
        ):
            raise ValueError(
                "prospective outcome portfolio universes differ"
            )
        for weights in (
            self.candidate_current_weights,
            self.candidate_target_weights,
            self.baseline_current_weights,
            self.baseline_target_weights,
        ):
            if (
                any(
                    not math.isfinite(value) or value < 0 or value > 1
                    for value in weights.values()
                )
                or sum(weights.values()) > 1 + self.numeric_tolerance
            ):
                raise ValueError(
                    "prospective outcome portfolio weights are invalid"
                )
        if any(
            not math.isfinite(value) or value <= -1
            for value in self.forward_returns.values()
        ):
            raise ValueError("prospective forward returns are invalid")
        if any(
            not math.isfinite(value) or value <= 0
            for value in self.adv_usd.values()
        ):
            raise ValueError("prospective ADV values are invalid")
        factor_ids = tuple(
            item.factor_id for item in self.known_factor_sources
        )
        if factor_ids != tuple(
            item.factor_id for item in self.known_factor_returns
        ):
            raise ValueError(
                "prospective outcome factor bindings differ"
            )
        by_key = {
            (item.session_date, item.symbol): item
            for item in self.source_bars
        }
        session_dates = tuple(
            sorted({item.session_date for item in self.source_bars})
        )
        expected_symbols = set(self.forward_returns)
        expected_symbols.update((self.market_symbol, self.sector_symbol))
        for factor in self.known_factor_sources:
            expected_symbols.update(
                (factor.long_symbol, factor.short_symbol)
            )
        if (
            len(session_dates) != 2
            or set(item.symbol for item in self.source_bars)
            != expected_symbols
            or len(self.source_bars) != 2 * len(expected_symbols)
        ):
            raise ValueError(
                "prospective outcome source-bar coverage is incomplete"
            )
        start_date, end_date = session_dates

        def source_return(symbol: str) -> float:
            start = by_key[(start_date, symbol)].adjusted_close
            end = by_key[(end_date, symbol)].adjusted_close
            return end / start - 1

        for symbol, value in self.forward_returns.items():
            if (
                abs(source_return(symbol) - value)
                > self.numeric_tolerance
            ):
                raise ValueError(
                    "prospective forward return is not source-bound"
                )
        if (
            abs(source_return(self.market_symbol) - self.market_return)
            > self.numeric_tolerance
            or abs(
                source_return(self.sector_symbol) - self.sector_return
            )
            > self.numeric_tolerance
        ):
            raise ValueError(
                "prospective market context is not source-bound"
            )
        factor_returns = {
            item.factor_id: item.return_value
            for item in self.known_factor_returns
        }
        for factor in self.known_factor_sources:
            expected = (
                source_return(factor.long_symbol)
                - source_return(factor.short_symbol)
            )
            if (
                abs(factor_returns[factor.factor_id] - expected)
                > self.numeric_tolerance
            ):
                raise ValueError(
                    "prospective factor return is not source-bound"
                )
        if self.outcome_available_at != max(
            item.available_at for item in self.source_bars
        ):
            raise ValueError(
                "prospective outcome availability is not source-bound"
            )
        source_payload = [
            item.model_dump(mode="python") for item in self.source_bars
        ]
        if canonical_hash(source_payload) != self.source_manifest_hash:
            raise ValueError(
                "prospective outcome source manifest hash mismatch"
            )
        payload = self.model_dump(mode="python", exclude={"outcome_hash"})
        if canonical_hash(payload) != self.outcome_hash:
            raise ValueError("prospective outcome hash mismatch")
        return self


class ProspectiveOutcomeFailureV1(DomainModel):
    schema_version: Literal["candidate_prospective_outcome_failure_v1"]
    failure_id: str = Field(pattern=IDENTIFIER_PATTERN)
    prospective_request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    execution_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    request_hash: str = Field(pattern=HASH_PATTERN)
    execution_hash: str = Field(pattern=HASH_PATTERN)
    implementation_calendar_session_id: str = Field(
        pattern=IDENTIFIER_PATTERN
    )
    evaluation_calendar_session_id: str = Field(
        pattern=IDENTIFIER_PATTERN
    )
    outcome_data_cutoff: datetime
    error_code: Literal["PROSPECTIVE_OUTCOME_DATA_WINDOW_MISSED"]
    config_manifest_hash: str = Field(pattern=HASH_PATTERN)
    created_at: datetime
    real_order_routing: Literal[False] = False
    automatic_promotion_enabled: Literal[False] = False
    challenger_lifecycle_advance_enabled: Literal[False] = False
    shadow_activation_enabled: Literal[False] = False
    broker_access_permitted: Literal[False] = False
    failure_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "outcome_data_cutoff",
        "created_at",
        mode="after",
    )
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_failure(self) -> Self:
        if self.created_at != self.outcome_data_cutoff:
            raise ValueError(
                "prospective outcome failure time must equal fixed cutoff"
            )
        payload = self.model_dump(mode="python", exclude={"failure_hash"})
        if canonical_hash(payload) != self.failure_hash:
            raise ValueError("prospective outcome failure hash mismatch")
        return self


def load_prospective_outcome_config(
    config_dir: Path,
) -> ProspectiveOutcomeConfigBundle:
    root = config_dir.resolve(strict=True)
    path = (root / PROSPECTIVE_OUTCOME_CONFIG_FILE).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("prospective outcome config escaped config root")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(
            "prospective outcome config must be a YAML object"
        )
    config = ProspectiveOutcomeConfigV1.model_validate(loaded)
    cost_path = (root / config.cost_model.config_path).resolve(strict=True)
    if not cost_path.is_relative_to(root):
        raise ValueError("prospective outcome cost config escaped config root")
    cost_text = cost_path.read_text(encoding="utf-8")
    normalized = cost_text.replace("\r\n", "\n").replace("\r", "\n")
    cost_sha = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if cost_sha != config.cost_model.config_content_sha256:
        raise ValueError(
            "prospective outcome cost config content SHA-256 mismatch"
        )
    loaded_cost_raw = yaml.safe_load(cost_text)
    if not isinstance(loaded_cost_raw, dict):
        raise ValueError("prospective outcome cost config must be an object")
    loaded_cost = cast(dict[str, object], loaded_cost_raw)
    if loaded_cost.get("version") != config.cost_model.version:
        raise ValueError("prospective outcome cost model version mismatch")
    commission = _nested_float(
        loaded_cost,
        "commission",
        "us_equity_rate",
    )
    half_spread = _nested_float(
        loaded_cost,
        "execution",
        "conservative_half_spread_bps",
    )
    delay = _nested_float(
        loaded_cost,
        "execution",
        "delay_penalty_bps",
    )
    if min(commission, half_spread, delay) < 0:
        raise ValueError("prospective outcome cost values cannot be negative")
    cost_hash = canonical_hash(loaded_cost)
    manifest_hash = canonical_hash(
        {
            PROSPECTIVE_OUTCOME_CONFIG_FILE: config,
            config.cost_model.config_path: loaded_cost,
        }
    )
    return ProspectiveOutcomeConfigBundle(
        config=config,
        commission_bps=commission * 10_000,
        spread_bps=half_spread * 2,
        delay_bps=delay,
        cost_model_hash=cost_hash,
        manifest_hash=manifest_hash,
        path=path,
        cost_path=cost_path,
    )


def build_prospective_outcome_evidence(
    *,
    request: ProspectiveRequestEvidenceV1,
    execution: ProspectiveExecutionEvidenceV1,
    config: ProspectiveOutcomeConfigBundle,
    implementation_calendar_session_id: str,
    evaluation_calendar_session_id: str,
    implementation_close_at: datetime,
    evaluation_close_at: datetime,
    outcome_data_cutoff: datetime,
    evaluation_nav_usd: float,
    candidate_current_weights: dict[str, float],
    candidate_target_weights: dict[str, float],
    baseline_current_weights: dict[str, float],
    baseline_target_weights: dict[str, float],
    forward_returns: dict[str, float],
    adv_usd: dict[str, float],
    market_return: float,
    sector_return: float,
    known_factor_returns: tuple[KnownFactorReturnV1, ...],
    regime: Literal["UP", "DOWN", "RANGE"],
    source_bars: tuple[ProspectiveOutcomeSourceBarV1, ...],
    created_at: datetime,
) -> ProspectiveOutcomeEvidenceV1:
    response = execution.primary_response
    if (
        response is None
        or not execution.deterministic_match
        or execution.prospective_request_id
        != request.prospective_request_id
        or execution.request_hash != request.request.request_hash
    ):
        raise ProspectiveOutcomeError(
            "PROSPECTIVE_OUTCOME_EXECUTION_BINDING_INVALID"
        )
    context = config.config.market_context
    expected_regime: Literal["UP", "DOWN", "RANGE"] = "RANGE"
    if market_return >= context.up_regime_return_threshold:
        expected_regime = "UP"
    elif market_return <= context.down_regime_return_threshold:
        expected_regime = "DOWN"
    if regime != expected_regime:
        raise ProspectiveOutcomeError(
            "PROSPECTIVE_OUTCOME_REGIME_BINDING_INVALID"
        )
    ordered_bars = tuple(
        sorted(
            source_bars,
            key=lambda item: (
                item.session_date,
                item.symbol,
                item.bar_id,
            ),
        )
    )
    source_manifest_hash = canonical_hash(
        [item.model_dump(mode="python") for item in ordered_bars]
    )
    outcome_available_at = max(item.available_at for item in ordered_bars)
    outcome_id = stable_id(
        "candidate-prospective-outcome",
        request.prospective_request_id,
        execution.execution_hash,
        implementation_calendar_session_id,
        evaluation_calendar_session_id,
        source_manifest_hash,
        config.manifest_hash,
    )
    payload = {
        "schema_version": "candidate_prospective_outcome_evidence_v1",
        "outcome_id": outcome_id,
        "prospective_request_id": request.prospective_request_id,
        "execution_id": execution.execution_id,
        "challenger_id": request.challenger_id,
        "candidate_artifact_hash": request.candidate_artifact_hash,
        "request_hash": request.request.request_hash,
        "execution_hash": execution.execution_hash,
        "decision_calendar_session_id": request.calendar_session_id,
        "implementation_calendar_session_id": (
            implementation_calendar_session_id
        ),
        "evaluation_calendar_session_id": evaluation_calendar_session_id,
        "calendar_version": config.config.calendar_version,
        "market_dataset_version": config.config.market_dataset_version,
        "timeframe": config.config.timeframe,
        "adjustment": config.config.adjustment,
        "decision_time": request.request.decision_time,
        "implementation_close_at": require_aware_utc(
            implementation_close_at
        ),
        "evaluation_close_at": require_aware_utc(evaluation_close_at),
        "outcome_data_cutoff": require_aware_utc(outcome_data_cutoff),
        "outcome_available_at": outcome_available_at,
        "evaluation_nav_usd": float(evaluation_nav_usd),
        "candidate_current_weights": dict(
            sorted(
                (symbol, float(weight))
                for symbol, weight in candidate_current_weights.items()
            )
        ),
        "candidate_target_weights": dict(
            sorted(
                (symbol, float(weight))
                for symbol, weight in candidate_target_weights.items()
            )
        ),
        "baseline_current_weights": dict(
            sorted(
                (symbol, float(weight))
                for symbol, weight in baseline_current_weights.items()
            )
        ),
        "baseline_target_weights": dict(
            sorted(
                (symbol, float(weight))
                for symbol, weight in baseline_target_weights.items()
            )
        ),
        "forward_returns": dict(
            sorted(
                (symbol, float(value))
                for symbol, value in forward_returns.items()
            )
        ),
        "adv_usd": dict(
            sorted(
                (symbol, float(value))
                for symbol, value in adv_usd.items()
            )
        ),
        "adv_lookback_completed_sessions": (
            config.config.capacity.adv_lookback_completed_sessions
        ),
        "market_symbol": config.config.market_context.market_symbol,
        "sector_symbol": config.config.market_context.sector_symbol,
        "known_factor_sources": (
            config.config.market_context.known_factors
        ),
        "market_return": float(market_return),
        "sector_return": float(sector_return),
        "known_factor_returns": tuple(
            sorted(known_factor_returns, key=lambda item: item.factor_id)
        ),
        "regime": regime,
        "commission_bps": config.commission_bps,
        "spread_bps": config.spread_bps,
        "delay_bps": config.delay_bps,
        "source_bars": ordered_bars,
        "source_manifest_hash": source_manifest_hash,
        "config_manifest_hash": config.manifest_hash,
        "cost_model_hash": config.cost_model_hash,
        "numeric_tolerance": config.config.readiness.numeric_tolerance,
        "created_at": require_aware_utc(created_at),
        "real_order_routing": False,
        "automatic_promotion_enabled": False,
        "challenger_lifecycle_advance_enabled": False,
        "shadow_activation_enabled": False,
        "broker_access_permitted": False,
    }
    return ProspectiveOutcomeEvidenceV1.model_validate(
        {**payload, "outcome_hash": canonical_hash(payload)}
    )


def build_prospective_outcome_failure(
    *,
    request: ProspectiveRequestEvidenceV1,
    execution: ProspectiveExecutionEvidenceV1,
    config: ProspectiveOutcomeConfigBundle,
    implementation_calendar_session_id: str,
    evaluation_calendar_session_id: str,
    outcome_data_cutoff: datetime,
) -> ProspectiveOutcomeFailureV1:
    if (
        execution.primary_response is None
        or not execution.deterministic_match
        or execution.prospective_request_id
        != request.prospective_request_id
        or execution.request_hash != request.request.request_hash
    ):
        raise ProspectiveOutcomeError(
            "PROSPECTIVE_OUTCOME_EXECUTION_BINDING_INVALID"
        )
    cutoff = require_aware_utc(outcome_data_cutoff)
    payload = {
        "schema_version": "candidate_prospective_outcome_failure_v1",
        "failure_id": stable_id(
            "candidate-prospective-outcome-failure",
            request.prospective_request_id,
            execution.execution_hash,
            implementation_calendar_session_id,
            evaluation_calendar_session_id,
            cutoff,
            config.manifest_hash,
        ),
        "prospective_request_id": request.prospective_request_id,
        "execution_id": execution.execution_id,
        "challenger_id": request.challenger_id,
        "candidate_artifact_hash": request.candidate_artifact_hash,
        "request_hash": request.request.request_hash,
        "execution_hash": execution.execution_hash,
        "implementation_calendar_session_id": (
            implementation_calendar_session_id
        ),
        "evaluation_calendar_session_id": (
            evaluation_calendar_session_id
        ),
        "outcome_data_cutoff": cutoff,
        "error_code": "PROSPECTIVE_OUTCOME_DATA_WINDOW_MISSED",
        "config_manifest_hash": config.manifest_hash,
        "created_at": cutoff,
        "real_order_routing": False,
        "automatic_promotion_enabled": False,
        "challenger_lifecycle_advance_enabled": False,
        "shadow_activation_enabled": False,
        "broker_access_permitted": False,
    }
    return ProspectiveOutcomeFailureV1.model_validate(
        {**payload, "failure_hash": canonical_hash(payload)}
    )


def _nested_float(
    source: dict[str, object],
    section: str,
    field: str,
) -> float:
    nested_raw = source.get(section)
    if not isinstance(nested_raw, dict):
        raise ValueError(
            f"prospective outcome cost section {section} is missing"
        )
    nested = cast(dict[str, object], nested_raw)
    value = nested.get(field)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(
            f"prospective outcome cost field {section}.{field} is invalid"
        )
    return float(value)
