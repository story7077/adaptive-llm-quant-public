from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from trading.data.q1_pit import Q1PointInTimeMarketData
from trading.persistence.models import MarketBarRow


class _FakeMarketRepository:
    def __init__(self, rows: list[MarketBarRow]) -> None:
        self._rows = rows

    def latest_bars(self, **_: object) -> list[MarketBarRow]:
        return list(self._rows)


def test_completed_adjusted_closes_preserve_database_decimal_exactly() -> None:
    event_time = datetime(2026, 7, 24, 20, tzinfo=UTC)
    exact_close = Decimal("123.123456789123")
    row = MarketBarRow(
        bar_id="bar-exact-decimal",
        provider="alpaca",
        feed="iex",
        symbol="QQQ",
        timeframe="1Day",
        event_time=event_time,
        provider_timestamp=event_time.isoformat(),
        available_at=event_time + timedelta(minutes=1),
        ingested_at=event_time + timedelta(minutes=1),
        source_kind="HISTORICAL_API",
        open=exact_close,
        high=exact_close,
        low=exact_close,
        close=exact_close,
        volume=Decimal("1000.0000000000"),
        vwap=exact_close,
        trade_count=1,
        request_id="request",
        payload_hash="a" * 64,
        raw_object_uri=None,
        payload_json={
            "_adjustment": "all",
            "_dataset_version": "q1-adjusted-v1",
        },
    )
    service = object.__new__(Q1PointInTimeMarketData)
    cast(Any, service)._repository = _FakeMarketRepository([row])

    series = service._completed_series(
        symbol="QQQ",
        current_session_date=date(2026, 7, 27),
        cutoff=event_time + timedelta(minutes=2),
        limit=1,
        dataset_version="q1-adjusted-v1",
    )

    assert series.adjusted_closes == (exact_close,)
    assert isinstance(series.adjusted_closes[0], Decimal)
    assert format(series.adjusted_closes[0], "f") == "123.123456789123"
