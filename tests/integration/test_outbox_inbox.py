from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from trading.domain.events import make_domain_event
from trading.persistence.models import (
    DomainEventRow,
    FeatureSnapshotRow,
    OutboxEventRow,
    ProcessedEventRow,
)
from trading.persistence.repositories import DomainEventRepository


def test_source_event_outbox_and_idempotent_consumer_survive_restart(seeded_demo) -> None:
    _, _, factory, _, _ = seeded_demo
    with factory() as session:
        event_count = session.scalar(select(func.count()).select_from(DomainEventRow))
        outbox_count = session.scalar(select(func.count()).select_from(OutboxEventRow))
        published_count = session.scalar(
            select(func.count())
            .select_from(OutboxEventRow)
            .where(OutboxEventRow.published_at.is_not(None))
        )
        processed_count = session.scalar(select(func.count()).select_from(ProcessedEventRow))
        feature_count = session.scalar(select(func.count()).select_from(FeatureSnapshotRow))
    assert event_count == 1
    assert outbox_count == 1
    assert published_count == 1
    assert processed_count == 1
    assert feature_count == 3


def test_domain_event_and_outbox_roll_back_together(sqlite_database) -> None:
    _, _, factory = sqlite_database
    instant = datetime(2026, 7, 20, 19, 40, tzinfo=UTC)
    event = make_domain_event(
        aggregate_type="TestAggregate",
        aggregate_id="aggregate_1",
        event_type="TestCreated",
        occurred_at=instant,
        available_at=instant,
        payload={"value": 1},
        correlation_id="rollback_test",
    )
    with factory() as session:
        DomainEventRepository(session).add_with_outbox(event, "test")
        session.flush()
        session.rollback()
    with factory() as session:
        event_count = session.scalar(select(func.count()).select_from(DomainEventRow))
        outbox_count = session.scalar(select(func.count()).select_from(OutboxEventRow))
    assert event_count == 0
    assert outbox_count == 0
