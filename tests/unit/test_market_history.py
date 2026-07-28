from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from trading.data.history import MarketHistoryService
from trading.domain.time import FrozenClock


class _Repository:
    def last_bar_event_time(self, **_: object) -> datetime:
        return datetime(2026, 7, 27, tzinfo=UTC)

    def latest_bars(self, **_: object) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                payload_json={
                    "_adjustment": "all",
                    "_dataset_version": "alpaca_iex_adjusted_all_v1",
                }
            )
        ]

    def append(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(bars=0)


class _Client:
    def __init__(self) -> None:
        self.start: datetime | None = None

    async def fetch_bars(self, **kwargs: object) -> list[object]:
        self.start = kwargs["start"]  # type: ignore[assignment]
        return []


def test_full_history_backfill_ignores_incremental_overlap() -> None:
    now = datetime(2026, 7, 28, tzinfo=UTC)
    client = _Client()
    service = MarketHistoryService(
        repository=_Repository(),  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        symbols=("QQQ",),
        clock=FrozenClock(now),
    )

    result = asyncio.run(
        service.backfill_daily(
            lookback_days=3650,
            force_full_lookback=True,
        )
    )

    assert result.start == datetime(2016, 7, 30, tzinfo=UTC)
    assert client.start == result.start


def test_incremental_history_keeps_ninety_day_overlap() -> None:
    now = datetime(2026, 7, 28, tzinfo=UTC)
    client = _Client()
    service = MarketHistoryService(
        repository=_Repository(),  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        symbols=("QQQ",),
        clock=FrozenClock(now),
    )

    result = asyncio.run(service.backfill_daily(lookback_days=3650))

    assert result.start == datetime(2026, 4, 28, tzinfo=UTC)
    assert client.start == result.start
