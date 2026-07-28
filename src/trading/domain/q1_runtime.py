from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.enums import OrderSide
from trading.domain.q1 import Q1ArmId
from trading.domain.time import require_aware_utc


class Q1OrderIntent(DomainModel):
    order_intent_id: str
    run_id: str
    arm_id: Q1ArmId
    portfolio_decision_id: str
    risk_decision_id: str
    source_cycle_id: str
    input_state_sequence: int = Field(ge=0)
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,29}$")
    side: OrderSide
    order_class: str
    quantity: Decimal = Field(gt=0)
    decision_quote_id: str
    decision_reference_price: Decimal = Field(gt=0)
    decision_spread_bps: Decimal = Field(ge=0)
    created_at: datetime
    valid_until: datetime
    idempotency_key: str
    algorithm_version: str
    config_manifest_hash: str
    code_version: str
    model_version: str
    source_manifest_hash: str
    intent_hash: str

    @field_validator("created_at", "valid_until", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_intent(self) -> Self:
        if self.valid_until <= self.created_at:
            raise ValueError("Q1 order must have a positive validity window")
        if (
            self.order_class
            in {
                "EMERGENCY_REDUCTION",
                "LLM_REDUCTION",
                "LIVE_MIRROR_TRANSITION",
            }
            and self.side is not OrderSide.SELL
        ):
            raise ValueError(f"{self.order_class} orders must be sell-only")
        return self


class Q1Fill(DomainModel):
    fill_id: str
    order_intent_id: str
    run_id: str
    arm_id: Q1ArmId
    source_cycle_id: str
    quote_id: str
    quote_event_time: datetime
    quote_available_at: datetime
    symbol: str
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    commission_usd: Decimal = Field(ge=0)
    cumulative_order_commission_usd: Decimal = Field(ge=0)
    execution_scenario_id: str
    base_fill_cost_usd: Decimal = Field(ge=0)
    sensitivity_5bp_cost_usd: Decimal = Field(ge=0)
    sensitivity_10bp_cost_usd: Decimal = Field(ge=0)
    effective_at: datetime
    created_at: datetime
    algorithm_version: str
    config_manifest_hash: str
    code_version: str
    model_version: str
    source_manifest_hash: str
    fill_hash: str

    @field_validator(
        "quote_event_time",
        "quote_available_at",
        "effective_at",
        "created_at",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_fill(self) -> Self:
        if self.quote_available_at > self.effective_at:
            raise ValueError("Fill cannot precede quote availability")
        if self.created_at < self.effective_at:
            raise ValueError("Fill creation cannot predate effective time")
        return self
