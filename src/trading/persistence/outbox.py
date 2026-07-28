from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.persistence.models import OutboxEventRow


class Outbox:
    def __init__(self, session: Session) -> None:
        self._session = session

    def pending(self, *, as_of: datetime, limit: int = 100) -> list[OutboxEventRow]:
        statement = (
            select(OutboxEventRow)
            .where(
                OutboxEventRow.published_at.is_(None),
                OutboxEventRow.next_attempt_at <= as_of,
            )
            .order_by(OutboxEventRow.outbox_id)
            .limit(limit)
        )
        return list(self._session.scalars(statement))

    def mark_published(self, row: OutboxEventRow, published_at: datetime) -> None:
        row.published_at = published_at
        row.attempt_count += 1

