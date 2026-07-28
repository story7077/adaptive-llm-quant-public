from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.domain.contracts import SourceRecord, model_payload
from trading.domain.events import DomainEvent
from trading.domain.hashing import stable_id
from trading.persistence.models import (
    DomainEventRow,
    OutboxEventRow,
    SourceRecordRow,
)


class DomainEventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_with_outbox(self, event: DomainEvent, topic: str) -> None:
        self._session.add(
            DomainEventRow(
                event_id=event.event_id,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                event_type=event.event_type,
                event_version=event.event_version,
                occurred_at=event.occurred_at,
                available_at=event.available_at,
                payload_json=model_payload(event)["payload"],
                payload_hash=event.payload_hash,
                causation_id=event.causation_id,
                correlation_id=event.correlation_id,
                idempotency_key=event.idempotency_key,
                created_at=event.created_at,
            )
        )
        self._session.add(
            OutboxEventRow(
                outbox_id=stable_id("outbox", event.event_id),
                event_id=event.event_id,
                topic=topic,
                payload_json=model_payload(event),
                published_at=None,
                attempt_count=0,
                next_attempt_at=event.available_at,
            )
        )

    def get(self, event_id: str) -> DomainEventRow | None:
        return self._session.get(DomainEventRow, event_id)


class SourceRecordRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, source: SourceRecord, *, payload: dict[str, object]) -> None:
        self._session.add(
            SourceRecordRow(
                source_id=source.source_id,
                provider=source.provider,
                external_id=source.external_id,
                revision=source.revision,
                published_at=source.published_at,
                available_at=source.available_at,
                content_hash=source.content_hash,
                payload_json={"record": model_payload(source), "content": payload},
            )
        )

    def get(self, source_id: str) -> SourceRecordRow | None:
        return self._session.get(SourceRecordRow, source_id)

    def get_for_run(self, run_id: str) -> SourceRecordRow | None:
        return self._session.scalar(
            select(SourceRecordRow).where(SourceRecordRow.external_id == run_id)
        )


def utc_or_none(value: datetime | None) -> datetime | None:
    return value

