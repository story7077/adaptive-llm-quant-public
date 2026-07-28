from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self, cast

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.time import require_aware_utc

Q1_OVERLAY_SCHEMA_VERSION = "q1_llm_overlay_v1"
Q1_RISKY_SYMBOLS = ("QQQ", "SOXX")
Q1_ALLOWED_RISK_MULTIPLIERS = (1.0, 0.75, 0.5)
Q1_TARGET_WEIGHT_SUM_TOLERANCE = Decimal("0.000000000001")
Q1_REQUEST_ID_MAX_LENGTH = 100
Q1_CONTEXT_HASH_HEX_LENGTH = 64
Q1_EVIDENCE_EVENT_IDS_MAX_LENGTH = 100
Q1_RATIONALE_MAX_LENGTH = 2000


class Q1OverlayState(StrEnum):
    NO_CHANGE = "NO_CHANGE"
    ACTIVE = "ACTIVE"
    EXPIRED_AWAITING_NEXT_REBALANCE = "EXPIRED_AWAITING_NEXT_REBALANCE"
    SUPERSEDED = "SUPERSEDED"


class Q1LlmOverlayDecision(DomainModel):
    schema_version: Literal["q1_llm_overlay_v1"] = Q1_OVERLAY_SCHEMA_VERSION
    request_id: str = Field(min_length=1, max_length=Q1_REQUEST_ID_MAX_LENGTH)
    context_manifest_hash: str = Field(
        pattern=rf"^[0-9a-f]{{{Q1_CONTEXT_HASH_HEX_LENGTH}}}$"
    )
    risk_multiplier: float
    block_new_entries: bool
    evidence_event_ids: list[str] = Field(
        max_length=Q1_EVIDENCE_EVENT_IDS_MAX_LENGTH
    )
    rationale: str = Field(min_length=1, max_length=Q1_RATIONALE_MAX_LENGTH)
    effective_time: datetime
    expiry_time: datetime
    created_at: datetime

    @field_validator(
        "effective_time",
        "expiry_time",
        "created_at",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("risk_multiplier", mode="after")
    @classmethod
    def validate_risk_multiplier(cls, value: float) -> float:
        if value not in Q1_ALLOWED_RISK_MULTIPLIERS:
            raise ValueError("Q1 overlay risk multiplier is outside the versioned schema")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.expiry_time <= self.effective_time:
            raise ValueError("Q1 overlay expiry must follow effective time")
        if self.created_at > self.effective_time:
            raise ValueError("Q1 overlay cannot become effective before creation")
        if (
            self.risk_multiplier < 1.0 or self.block_new_entries
        ) and not self.evidence_event_ids:
            raise ValueError("Risk-reducing Q1 overlay requires bounded evidence")
        return self


def apply_reduce_only_overlay(
    deterministic_target: dict[str, float],
    *,
    current_weights: dict[str, float],
    decision: Q1LlmOverlayDecision | None,
    as_of: datetime,
) -> tuple[dict[str, float], Q1OverlayState]:
    base = _normalize_target(deterministic_target)
    if decision is None:
        return base, Q1OverlayState.NO_CHANGE
    instant = require_aware_utc(as_of)
    if instant < decision.effective_time:
        return base, Q1OverlayState.NO_CHANGE
    if instant >= decision.expiry_time:
        return (
            _normalize_target(current_weights),
            Q1OverlayState.EXPIRED_AWAITING_NEXT_REBALANCE,
        )

    target: dict[str, float] = {}
    for symbol in Q1_RISKY_SYMBOLS:
        deterministic_weight = base.get(symbol, 0.0)
        reduced = deterministic_weight * decision.risk_multiplier
        if decision.block_new_entries:
            reduced = min(reduced, max(0.0, current_weights.get(symbol, 0.0)))
        target[symbol] = min(deterministic_weight, max(0.0, reduced))
    target["USD_CASH"] = 1.0 - sum(target.values())
    return target, Q1OverlayState.ACTIVE


def validate_bounded_evidence(
    decision: Q1LlmOverlayDecision,
    *,
    allowed_event_ids: set[str],
) -> None:
    outside = sorted(set(decision.evidence_event_ids) - allowed_event_ids)
    if outside:
        raise ValueError(f"Q1 overlay cited evidence outside the request: {outside}")


def _normalize_target(target: dict[str, float]) -> dict[str, float]:
    allowed = {*Q1_RISKY_SYMBOLS, "USD_CASH"}
    unknown = sorted(set(target) - allowed)
    if unknown:
        raise ValueError(f"Q1 overlay target contains unknown symbols: {unknown}")
    values = {
        symbol: max(0.0, float(target.get(symbol, 0.0)))
        for symbol in Q1_RISKY_SYMBOLS
    }
    risky = sum(values.values())
    if Decimal(str(risky)) > Decimal("1") + Q1_TARGET_WEIGHT_SUM_TOLERANCE:
        raise ValueError("Q1 deterministic target is leveraged")
    values["USD_CASH"] = 1.0 - risky
    return values


def validate_q1_overlay_config(document: dict[str, Any]) -> None:
    """Ensure trading parameters in the schema match the versioned run config."""

    raw_llm = document.get("llm")
    if not isinstance(raw_llm, dict):
        raise ValueError("Q1 llm config must be an object")
    llm = cast(dict[str, Any], raw_llm)
    raw_multipliers = llm.get("allowed_risk_multipliers")
    if not isinstance(raw_multipliers, list):
        raise ValueError("Q1 allowed_risk_multipliers must be a list")
    multipliers = tuple(
        float(value)
        for value in cast(list[Any], raw_multipliers)
    )
    if multipliers != Q1_ALLOWED_RISK_MULTIPLIERS:
        raise ValueError("Q1 LLM multiplier config differs from its versioned schema")
    raw_tolerance = llm.get("target_weight_sum_tolerance")
    if raw_tolerance is None or isinstance(raw_tolerance, bool):
        raise ValueError("Q1 target_weight_sum_tolerance must be numeric")
    tolerance = Decimal(str(raw_tolerance))
    if tolerance != Q1_TARGET_WEIGHT_SUM_TOLERANCE:
        raise ValueError("Q1 LLM target-weight tolerance differs from its schema")
    raw_maximum_evidence = llm.get("maximum_evidence_events")
    if (
        isinstance(raw_maximum_evidence, bool)
        or not isinstance(raw_maximum_evidence, int)
        or raw_maximum_evidence != Q1_EVIDENCE_EVENT_IDS_MAX_LENGTH
    ):
        raise ValueError(
            "Q1 maximum_evidence_events must match the versioned schema"
        )
    timeout_values: dict[str, Decimal] = {}
    for key in (
        "provider_timeout_seconds",
        "transport_timeout_seconds",
        "transport_poll_interval_seconds",
    ):
        raw_value = llm.get(key)
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, int | float)
        ):
            raise ValueError(f"Q1 {key} must be numeric")
        value = Decimal(str(raw_value))
        if not value.is_finite() or value <= 0:
            raise ValueError(f"Q1 {key} must be positive")
        timeout_values[key] = value
    if (
        timeout_values["transport_timeout_seconds"]
        >= timeout_values["provider_timeout_seconds"]
    ):
        raise ValueError(
            "Q1 transport_timeout_seconds must be shorter than "
            "provider_timeout_seconds"
        )
    if (
        timeout_values["transport_poll_interval_seconds"]
        > timeout_values["transport_timeout_seconds"]
    ):
        raise ValueError(
            "Q1 transport_poll_interval_seconds must not exceed "
            "transport_timeout_seconds"
        )
