from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue, field_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc


class DomainEvent(DomainModel):
    event_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    event_version: str
    occurred_at: datetime
    available_at: datetime
    payload: dict[str, JsonValue]
    payload_hash: str
    causation_id: str | None
    correlation_id: str
    idempotency_key: str = Field(min_length=1)
    created_at: datetime

    @field_validator("occurred_at", "available_at", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)


def make_domain_event(
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    occurred_at: datetime,
    available_at: datetime,
    payload: dict[str, JsonValue],
    correlation_id: str,
    causation_id: str | None = None,
    event_version: str = "1",
) -> DomainEvent:
    payload_hash = canonical_hash(payload)
    event_id = stable_id("evt", event_type, aggregate_id, payload_hash)
    return DomainEvent(
        event_id=event_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        event_version=event_version,
        occurred_at=occurred_at,
        available_at=available_at,
        payload=payload,
        payload_hash=payload_hash,
        causation_id=causation_id,
        correlation_id=correlation_id,
        idempotency_key=stable_id("idem", event_type, aggregate_id, payload_hash),
        created_at=available_at,
    )
