from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from trading.persistence.models import ProcessedEventRow


class ProcessedEventInbox:
    def __init__(self, session: Session, consumer_name: str) -> None:
        self._session = session
        self._consumer_name = consumer_name

    def seen(self, event_id: str) -> bool:
        return (
            self._session.get(
                ProcessedEventRow,
                {"consumer_name": self._consumer_name, "event_id": event_id},
            )
            is not None
        )

    def record(self, event_id: str, processed_at: datetime, result_hash: str) -> bool:
        if self.seen(event_id):
            return False
        self._session.add(
            ProcessedEventRow(
                consumer_name=self._consumer_name,
                event_id=event_id,
                processed_at=processed_at,
                result_hash=result_hash,
            )
        )
        return True

