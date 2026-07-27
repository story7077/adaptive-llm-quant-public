from __future__ import annotations

from datetime import datetime
from typing import Protocol

from trading.domain.contracts import FeatureSnapshot


class PointInTimeRecord(Protocol):
    @property
    def available_at(self) -> datetime: ...


class MarketDataPort(Protocol):
    def feature_snapshot(self, symbol: str, as_of: datetime) -> FeatureSnapshot: ...


def available_as_of[T](records: list[T], as_of: datetime) -> list[T]:
    selected: list[T] = []
    for record in records:
        available_at = getattr(record, "available_at", None)
        if not isinstance(available_at, datetime):
            raise TypeError("PIT record must expose datetime available_at")
        if available_at <= as_of:
            selected.append(record)
    return selected
