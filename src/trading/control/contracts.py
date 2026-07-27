from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from trading.control.providers import CommanderProvider
from trading.domain.contracts import DomainModel, PolicyOperation, TypedCondition
from trading.domain.time import require_aware_utc

REQUEST_SCHEMA_VERSION = "adaptive_control_request_v1"
HASH_PATTERN = r"^[0-9a-f]{64}$"


class DecisionKind(StrEnum):
    NO_CHANGE = "NO_CHANGE"
    APPLY_PATCH = "APPLY_PATCH"


class CommanderRequest(DomainModel):
    schema_version: Literal["adaptive_control_request_v1"] = REQUEST_SCHEMA_VERSION
    request_id: str = Field(min_length=1, max_length=100)
    selection_version: int = Field(ge=1)
    provider: CommanderProvider
    model: str
    reasoning_profile: str
    scope_id: str = Field(default="legacy_global", min_length=1, max_length=80)
    arm_scope: Literal["B3-RISK", "B3-FULL"]
    base_policy_version: int = Field(ge=0)
    as_of: datetime
    data_available_cutoff: datetime
    expires_at: datetime
    context: dict[str, JsonValue]
    active_policy: dict[str, JsonValue]
    context_manifest_hash: str = Field(pattern=HASH_PATTERN)
    prompt_hash: str = Field(pattern=HASH_PATTERN)
    created_at: datetime

    @field_validator(
        "as_of",
        "data_available_cutoff",
        "expires_at",
        "created_at",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.data_available_cutoff > self.as_of:
            raise ValueError("Request data_available_cutoff must not exceed as_of")
        if self.expires_at <= self.created_at:
            raise ValueError("Request expires_at must be after created_at")
        return self


class AdaptivePolicyDecision(DomainModel):
    schema_version: Literal["adaptive_policy_decision_v1"]
    request_id: str = Field(min_length=1, max_length=100)
    context_manifest_hash: str = Field(pattern=HASH_PATTERN)
    decision: DecisionKind
    arm_scope: Literal["B3-RISK", "B3-FULL"]
    base_policy_version: int = Field(ge=0)
    effective_from: datetime | None
    expires_at: datetime | None
    operations: list[PolicyOperation] = Field(max_length=20)
    evidence_news_event_ids: list[str] = Field(max_length=100)
    raw_confidence: float = Field(ge=0, le=1)
    rollback_conditions: list[TypedCondition] = Field(max_length=20)
    rationale_summary: str = Field(min_length=1, max_length=2000)

    @field_validator("effective_from", "expires_at", mode="after")
    @classmethod
    def validate_optional_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware_utc(value)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.decision is DecisionKind.NO_CHANGE:
            if self.effective_from is not None or self.expires_at is not None:
                raise ValueError("NO_CHANGE must not include an effective window")
            if self.operations or self.rollback_conditions:
                raise ValueError("NO_CHANGE must not include policy operations")
            return self

        if self.effective_from is None or self.expires_at is None:
            raise ValueError("APPLY_PATCH requires effective_from and expires_at")
        if self.expires_at <= self.effective_from:
            raise ValueError("expires_at must be after effective_from")
        if not self.operations:
            raise ValueError("APPLY_PATCH requires at least one operation")
        if not self.evidence_news_event_ids:
            raise ValueError("APPLY_PATCH requires news-event evidence")
        return self


class SelectionSnapshot(DomainModel):
    selection_id: str
    version: int = Field(ge=1)
    provider: CommanderProvider
    model: str
    reasoning_profile: str
    created_at: datetime
    config_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return require_aware_utc(value)


class DecisionReceipt(DomainModel):
    decision_id: str
    request_id: str
    provider: CommanderProvider
    status: Literal["ACCEPTED", "REJECTED", "NO_CHANGE"]
    reason_code: str
    reason_detail: str
    arm_scope: Literal["B3-RISK", "B3-FULL"]
    base_policy_version: int = Field(ge=0)
    applied_policy_version: int | None = Field(default=None, ge=0)
    compiled_policy_hash: str | None = None
    created_at: datetime
    idempotent_replay: bool = False

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return require_aware_utc(value)
