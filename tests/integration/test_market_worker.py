from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from trading.data.alpaca import (
    AlpacaHttpError,
    AlpacaStreamError,
    StreamFrame,
    parse_stream_message,
)
from trading.data.market_repository import MarketDataRepository
from trading.data.worker import AlpacaMarketWorker
from trading.domain.contracts import MarketBar
from trading.domain.time import FrozenClock


def test_backfill_overlaps_last_complete_bar_by_two_minutes(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = MarketDataRepository(factory)
    repository.append(bars=[_bar()])
    rest = FakeRest()
    worker = AlpacaMarketWorker(
        repository=repository,
        rest_client=rest,  # type: ignore[arg-type]
        stream_client=EmptyStream(),  # type: ignore[arg-type]
        symbols=("SOXL",),
        clock=FrozenClock(datetime(2026, 7, 27, 15, 0, tzinfo=UTC)),
    )
    result = asyncio.run(worker.backfill_once(lookback=timedelta(days=1)))

    assert result.start == datetime(2026, 7, 27, 14, 28, tzinfo=UTC)
    assert rest.starts == [result.start]


def test_auth_failure_stops_without_retrying(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = MarketDataRepository(factory)
    rest = AuthFailureRest()
    worker = AlpacaMarketWorker(
        repository=repository,
        rest_client=rest,  # type: ignore[arg-type]
        stream_client=EmptyStream(),  # type: ignore[arg-type]
        symbols=("SOXL",),
        clock=FrozenClock(datetime(2026, 7, 27, 15, 0, tzinfo=UTC)),
    )
    asyncio.run(worker.run_forever())

    status = repository.status(provider="alpaca", feed="iex")
    assert status is not None
    assert status.state == "AUTH_REQUIRED"
    assert status.consecutive_failures == 1
    assert rest.closed is True


def test_transient_stream_failure_reconnects_and_dedupes_live_quote(
    sqlite_database,
    monkeypatch,
) -> None:
    _, _, factory = sqlite_database
    repository = MarketDataRepository(factory)
    stop = asyncio.Event()
    stream = RecoveringStream(stop)

    async def no_delay(_stop: asyncio.Event, _seconds: float) -> None:
        return None

    monkeypatch.setattr("trading.data.worker._wait_or_stop", no_delay)
    worker = AlpacaMarketWorker(
        repository=repository,
        rest_client=FakeRest(),  # type: ignore[arg-type]
        stream_client=stream,  # type: ignore[arg-type]
        symbols=("SOXL",),
        clock=FrozenClock(datetime(2026, 7, 27, 15, 0, tzinfo=UTC)),
    )
    asyncio.run(worker.run_forever(stop))

    status = repository.status(provider="alpaca", feed="iex")
    quote = repository.latest_quote(
        provider="alpaca",
        feed="iex",
        symbol="SOXL",
        as_of=datetime(2026, 7, 27, 15, 0, 1, tzinfo=UTC),
    )
    assert status is not None
    assert status.reconnect_count == 1
    assert status.state == "STOPPED"
    assert quote is not None
    assert stream.calls == 2


def test_connected_stream_updates_worker_heartbeat(
    sqlite_database,
    monkeypatch,
) -> None:
    _, _, factory = sqlite_database
    repository = MarketDataRepository(factory)
    heartbeat_calls: list[datetime] = []
    original_heartbeat = repository.heartbeat

    def counted_heartbeat(**kwargs: Any) -> None:
        heartbeat_calls.append(kwargs["now"])
        original_heartbeat(**kwargs)

    monkeypatch.setattr(repository, "heartbeat", counted_heartbeat)
    stop = asyncio.Event()
    worker = AlpacaMarketWorker(
        repository=repository,
        rest_client=FakeRest(),  # type: ignore[arg-type]
        stream_client=HeartbeatStream(stop),  # type: ignore[arg-type]
        symbols=("SOXL",),
        clock=FrozenClock(datetime(2026, 7, 27, 15, 0, tzinfo=UTC)),
        heartbeat_interval_seconds=0.01,
    )
    asyncio.run(worker.run_forever(stop))

    assert heartbeat_calls


class FakeRest:
    def __init__(self) -> None:
        self.starts: list[datetime] = []
        self.closed = False

    async def fetch_bars(self, **kwargs: Any) -> list[Any]:
        self.starts.append(kwargs["start"])
        return []

    async def fetch_latest_quotes(self, **_kwargs: Any) -> list[Any]:
        return []

    async def fetch_latest_trades(self, **_kwargs: Any) -> list[Any]:
        return []

    async def aclose(self) -> None:
        self.closed = True


class AuthFailureRest(FakeRest):
    async def fetch_bars(self, **kwargs: Any) -> list[Any]:
        self.starts.append(kwargs["start"])
        raise AlpacaHttpError(401, "authentication failed")


class EmptyStream:
    async def frames(self):
        if False:
            yield None


class RecoveringStream:
    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.calls = 0

    async def frames(self):
        self.calls += 1
        if self.calls == 1:
            raise AlpacaStreamError(500, "temporary disconnect")
        yield StreamFrame(
            received_at=datetime(2026, 7, 27, 15, 0, tzinfo=UTC),
            raw_object_uri=None,
            messages=[],
            connected=True,
        )
        quote_message = {
            "T": "q",
            "S": "SOXL",
            "bx": "V",
            "bp": "42.29",
            "bs": 3,
            "ax": "V",
            "ap": "42.31",
            "as": 5,
            "c": [],
            "t": "2026-07-27T14:59:59Z",
            "z": "C",
        }
        yield StreamFrame(
            received_at=datetime(2026, 7, 27, 15, 0, tzinfo=UTC),
            raw_object_uri="raw://quote",
            messages=[quote_message],
        )
        self.stop.set()


class HeartbeatStream:
    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop

    async def frames(self):
        yield StreamFrame(
            received_at=datetime(2026, 7, 27, 15, 0, tzinfo=UTC),
            raw_object_uri=None,
            messages=[],
            connected=True,
        )
        await asyncio.sleep(0.04)
        self.stop.set()


def _bar() -> MarketBar:
    event = parse_stream_message(
        {
            "T": "b",
            "S": "SOXL",
            "o": "42.10",
            "h": "42.40",
            "l": "42.00",
            "c": "42.20",
            "v": 1200,
            "vw": "42.22",
            "n": 73,
            "t": "2026-07-27T14:30:00Z",
        },
        available_at=datetime(2026, 7, 27, 14, 31, tzinfo=UTC),
        raw_object_uri="raw://bar",
    )
    assert isinstance(event, MarketBar)
    return event
