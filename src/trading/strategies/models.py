from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.time import require_aware_utc


class ForecastCalibration(DomainModel):
    """Versioned OOS calibration available before a strategy decision."""

    strategy_id: str
    calibration_version: str
    trained_through: datetime
    available_at: datetime
    intercept_bps: Decimal
    slope_bps_per_signal: Decimal
    forecast_error_sd_bps: Decimal = Field(gt=0)
    effective_sample_size: Decimal = Field(ge=0)
    shrinkage: Decimal = Field(ge=0, le=1)

    @field_validator("trained_through", "available_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.available_at < self.trained_through:
            raise ValueError("Calibration cannot be available before its training cutoff")
        return self


class StrategyForecastContext(DomainModel):
    experiment_version: str
    reference_portfolio_id: str
    expires_at: datetime
    created_at: datetime
    calibration: ForecastCalibration
    standalone_expected_cost_bps: Decimal = Field(ge=0)
    capacity_usd: Decimal = Field(ge=0)
    health_multiplier: Decimal = Field(ge=0, le=1)
    code_commit: str
    min_edge_to_cost_ratio: Decimal = Field(default=Decimal("1.5"), ge=0)

    @field_validator("expires_at", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)
