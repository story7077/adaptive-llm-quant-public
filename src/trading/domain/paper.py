from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.time import require_aware_utc


def _reject_binary_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("Paper-account decimal values must not be binary floats")
    return value


def _require_scale(value: Decimal, *, max_places: int, field_name: str) -> Decimal:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError(f"{field_name} must be a finite decimal")
    fractional_places = max(0, -exponent)
    if fractional_places > max_places:
        raise ValueError(
            f"{field_name} supports at most {max_places} decimal places"
        )
    return value


class PaperCashSpec(DomainModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount: Decimal = Field(ge=0)
    tradable: bool
    exclusion_reason: str | None

    @field_validator("amount", mode="before")
    @classmethod
    def preserve_decimal_input(cls, value: Any) -> Any:
        return _reject_binary_float(value)

    @field_validator("amount", mode="after")
    @classmethod
    def validate_scale(cls, value: Decimal) -> Decimal:
        return _require_scale(value, max_places=10, field_name="amount")

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.tradable and self.exclusion_reason is not None:
            raise ValueError("Tradable cash cannot have an exclusion_reason")
        if not self.tradable and not self.exclusion_reason:
            raise ValueError("Non-tradable cash requires an exclusion_reason")
        return self


class PaperPositionSpec(DomainModel):
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,29}$")
    quantity: Decimal = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @field_validator("quantity", mode="before")
    @classmethod
    def preserve_decimal_input(cls, value: Any) -> Any:
        return _reject_binary_float(value)

    @field_validator("quantity", mode="after")
    @classmethod
    def validate_scale(cls, value: Decimal) -> Decimal:
        return _require_scale(value, max_places=10, field_name="quantity")


class PaperAccountSpec(DomainModel):
    schema_version: str = Field(pattern=r"^paper-account\.v[1-9][0-9]*$")
    account_id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)
    base_currency: str = Field(pattern=r"^[A-Z]{3}$")
    source: str = Field(min_length=1, max_length=40)
    cash: list[PaperCashSpec] = Field(min_length=1)
    positions: list[PaperPositionSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_account(self) -> Self:
        cash_currencies = [item.currency for item in self.cash]
        if len(cash_currencies) != len(set(cash_currencies)):
            raise ValueError("Cash currencies must be unique")
        position_symbols = [item.symbol for item in self.positions]
        if len(position_symbols) != len(set(position_symbols)):
            raise ValueError("Position symbols must be unique")
        base_cash = next(
            (item for item in self.cash if item.currency == self.base_currency),
            None,
        )
        if base_cash is None or not base_cash.tradable:
            raise ValueError("Base-currency cash must exist and be tradable")
        if any(item.currency != self.base_currency for item in self.positions):
            raise ValueError("Paper positions must be denominated in base_currency")
        return self


class PaperBootstrapMark(DomainModel):
    bootstrap_mark_id: str
    run_id: str
    account_spec_id: str
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,29}$")
    price: Decimal = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    marked_at: datetime
    source_kind: str = Field(min_length=1, max_length=40)
    source_record_id: str | None
    payload_hash: str
    created_at: datetime

    @field_validator("price", mode="before")
    @classmethod
    def preserve_decimal_input(cls, value: Any) -> Any:
        return _reject_binary_float(value)

    @field_validator("price", mode="after")
    @classmethod
    def validate_scale(cls, value: Decimal) -> Decimal:
        return _require_scale(value, max_places=12, field_name="price")

    @field_validator("marked_at", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.created_at < self.marked_at:
            raise ValueError("Bootstrap mark cannot be created before it is observed")
        return self


class PaperBootstrapCompletion(DomainModel):
    bootstrap_completion_id: str
    run_id: str
    account_spec_id: str
    common_mark_at: datetime
    initial_nav_usd: Decimal = Field(gt=0)
    input_manifest_hash: str
    mark_ids: list[str] = Field(min_length=1)
    completed_at: datetime

    @field_validator("initial_nav_usd", mode="before")
    @classmethod
    def preserve_decimal_input(cls, value: Any) -> Any:
        return _reject_binary_float(value)

    @field_validator("initial_nav_usd", mode="after")
    @classmethod
    def validate_scale(cls, value: Decimal) -> Decimal:
        return _require_scale(
            value,
            max_places=10,
            field_name="initial_nav_usd",
        )

    @field_validator("common_mark_at", "completed_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_completion(self) -> Self:
        if self.completed_at < self.common_mark_at:
            raise ValueError("Bootstrap cannot complete before the common mark")
        if len(self.mark_ids) != len(set(self.mark_ids)):
            raise ValueError("Bootstrap mark_ids must be unique")
        return self
