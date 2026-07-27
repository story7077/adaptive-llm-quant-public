from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel, FeatureSnapshot, FeatureValue
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc


class AdjustedPriceObservation(DomainModel):
    """A split/dividend-adjusted point-in-time mark for one US trading session.

    ``session_date`` is explicit because vendor timestamps for daily and intraday
    records do not consistently map to the US session date. The upstream adapter
    owns adjustment methodology and records its evidence in ``source_record_id``.
    """

    source_record_id: str
    symbol: str
    session_date: date
    event_time: datetime
    available_at: datetime
    adjusted_price: Decimal = Field(gt=0)

    @field_validator("event_time", "available_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.available_at < self.event_time:
            raise ValueError("Adjusted price available_at cannot precede event_time")
        return self


class IndexMembership(DomainModel):
    """Point-in-time membership interval announced by the index data source."""

    source_record_id: str
    universe_id: str
    symbol: str
    effective_from: date
    effective_to: date | None = None
    available_at: datetime

    @field_validator("available_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("Index membership effective_to must follow effective_from")
        return self

    def is_effective(self, session_date: date) -> bool:
        return self.effective_from <= session_date and (
            self.effective_to is None or session_date < self.effective_to
        )


class ScheduledEventWindow(DomainModel):
    """A known-before-decision trading blackout window."""

    source_record_id: str
    event_type: str
    starts_at: datetime
    ends_at: datetime
    available_at: datetime
    affected_symbols: tuple[str, ...] = ()

    @field_validator("starts_at", "ends_at", "available_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("Scheduled event ends_at must follow starts_at")
        return self

    def blocks(self, symbol: str, decision_time: datetime) -> bool:
        applies = not self.affected_symbols or symbol in self.affected_symbols
        return applies and self.starts_at <= decision_time <= self.ends_at


class PortfolioWeightSnapshot(DomainModel):
    """Versioned long-only weights made available before a strategy decision."""

    snapshot_id: str
    portfolio_id: str
    as_of: datetime
    available_at: datetime
    weights: dict[str, Decimal]

    @field_validator("as_of", "available_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_weights(self) -> Self:
        if self.available_at < self.as_of:
            raise ValueError("Portfolio weights cannot be available before as_of")
        if any(weight < 0 for weight in self.weights.values()):
            raise ValueError("Portfolio weights must be long-only")
        if abs(sum(self.weights.values(), Decimal("0")) - Decimal("1")) > Decimal("1e-12"):
            raise ValueError("Portfolio weights must sum to one")
        return self


class FeatureBuildContext(DomainModel):
    decision_time: datetime
    data_available_cutoff: datetime
    created_at: datetime
    feature_set_version: str

    @field_validator("decision_time", "data_available_cutoff", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_cutoff(self) -> Self:
        if self.data_available_cutoff > self.decision_time:
            raise ValueError("Feature cutoff cannot exceed decision_time")
        if self.created_at < self.data_available_cutoff:
            raise ValueError("Feature created_at cannot precede its data cutoff")
        return self


@dataclass(frozen=True, slots=True)
class FeatureBuildResult:
    """Safe scheduler-facing outcome.

    Missing or unsafe data produces reason codes and no snapshot. A scheduler can
    record the block without accidentally turning partial inputs into a signal.
    """

    strategy_id: str
    symbol: str | None
    snapshot: FeatureSnapshot | None
    reason_codes: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.snapshot is not None and not self.reason_codes


def blocked_result(
    strategy_id: str,
    symbol: str | None,
    *reason_codes: str,
) -> FeatureBuildResult:
    return FeatureBuildResult(
        strategy_id=strategy_id,
        symbol=symbol,
        snapshot=None,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


def feature_snapshot(
    *,
    strategy_id: str,
    symbol: str | None,
    context: FeatureBuildContext,
    feature_code_version: str,
    values: list[tuple[str, Decimal, str, list[str]]],
    manifest: object,
) -> FeatureBuildResult:
    manifest_hash = canonical_hash(manifest)
    snapshot_id = stable_id(
        "feat",
        strategy_id,
        symbol,
        context.decision_time,
        context.feature_set_version,
        manifest_hash,
    )
    snapshot = FeatureSnapshot(
        feature_snapshot_id=snapshot_id,
        symbol=symbol,
        decision_time=context.decision_time,
        data_available_cutoff=context.data_available_cutoff,
        feature_set_version=context.feature_set_version,
        values=[
            FeatureValue(
                name=name,
                value=float(value),
                unit=unit,
                source_record_ids=sorted(set(source_ids)),
                feature_code_version=feature_code_version,
            )
            for name, value, unit, source_ids in values
        ],
        input_manifest_hash=manifest_hash,
        created_at=context.created_at,
    )
    return FeatureBuildResult(strategy_id=strategy_id, symbol=symbol, snapshot=snapshot)
