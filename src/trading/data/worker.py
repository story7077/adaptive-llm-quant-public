from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

from trading.data.alpaca import (
    FEED,
    PROVIDER,
    AlpacaHttpError,
    AlpacaRestClient,
    AlpacaStreamClient,
    AlpacaStreamError,
    StreamFrame,
    parse_stream_message,
)
from trading.data.market_repository import IngestCounts, MarketDataRepository
from trading.domain.contracts import MarketBar, MarketQuote, MarketTradeEvent
from trading.domain.enums import MarketConnectionState
from trading.domain.time import Clock, SystemClock


@dataclass(frozen=True, slots=True)
class BackfillResult:
    start: datetime
    end: datetime
    counts: IngestCounts


class AlpacaMarketWorker:
    def __init__(
        self,
        *,
        repository: MarketDataRepository,
        rest_client: AlpacaRestClient,
        stream_client: AlpacaStreamClient,
        symbols: tuple[str, ...],
        clock: Clock | None = None,
        initial_lookback: timedelta = timedelta(days=7),
        reconnect_overlap: timedelta = timedelta(minutes=2),
        heartbeat_interval_seconds: float = 10.0,
        random_source: random.Random | None = None,
    ) -> None:
        self._repository = repository
        self._rest_client = rest_client
        self._stream_client = stream_client
        self._symbols = symbols
        self._clock = clock or SystemClock()
        self._initial_lookback = initial_lookback
        self._reconnect_overlap = reconnect_overlap
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._random = random_source or random.Random()

    async def backfill_once(
        self,
        *,
        lookback: timedelta | None = None,
    ) -> BackfillResult:
        end = self._clock.now()
        latest = await asyncio.to_thread(
            self._repository.last_bar_event_time,
            provider=PROVIDER,
            feed=FEED,
            symbols=self._symbols,
            timeframe="1Min",
        )
        requested_lookback = lookback or self._initial_lookback
        start = (
            end - requested_lookback
            if latest is None
            else max(end - requested_lookback, latest - self._reconnect_overlap)
        )
        bars_task = self._rest_client.fetch_bars(
            symbols=self._symbols,
            start=start,
            end=end,
        )
        quotes_task = self._rest_client.fetch_latest_quotes(symbols=self._symbols)
        trades_task = self._rest_client.fetch_latest_trades(symbols=self._symbols)
        bars, quotes, trades = await asyncio.gather(
            bars_task,
            quotes_task,
            trades_task,
        )
        counts = await asyncio.to_thread(
            self._repository.append,
            bars=bars,
            quotes=quotes,
            trades=trades,
        )
        await asyncio.to_thread(
            self._repository.record_received,
            provider=PROVIDER,
            feed=FEED,
            received_at=end,
            bar_at=_latest_time(bars),
            quote_at=_latest_time(quotes),
            trade_at=_latest_time(trades),
        )
        return BackfillResult(start=start, end=end, counts=counts)

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        attempt = 0
        cancelled = False
        await asyncio.to_thread(
            self._repository.transition,
            provider=PROVIDER,
            feed=FEED,
            state=MarketConnectionState.CONNECTING,
            now=self._clock.now(),
        )
        try:
            while not stop.is_set():
                try:
                    await self.backfill_once()
                    await self._stream_once(stop)
                    if not stop.is_set():
                        raise AlpacaStreamError("CLOSED", "Stream ended unexpectedly")
                    attempt = 0
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                except (AlpacaHttpError, AlpacaStreamError) as exc:
                    if _is_auth_error(exc):
                        await asyncio.to_thread(
                            self._repository.transition,
                            provider=PROVIDER,
                            feed=FEED,
                            state=MarketConnectionState.AUTH_REQUIRED,
                            now=self._clock.now(),
                            error_code=exc.error_code,
                            error_detail=str(exc),
                            failed=True,
                        )
                        return
                    attempt += 1
                    await asyncio.to_thread(
                        self._repository.transition,
                        provider=PROVIDER,
                        feed=FEED,
                        state=MarketConnectionState.RECONNECTING,
                        now=self._clock.now(),
                        error_code=exc.error_code,
                        error_detail=str(exc),
                        increment_reconnect=True,
                        failed=True,
                    )
                    await _wait_or_stop(stop, _backoff_seconds(attempt, self._random))
                except Exception as exc:
                    attempt += 1
                    await asyncio.to_thread(
                        self._repository.transition,
                        provider=PROVIDER,
                        feed=FEED,
                        state=MarketConnectionState.RECONNECTING,
                        now=self._clock.now(),
                        error_code=type(exc).__name__.upper(),
                        error_detail=str(exc),
                        increment_reconnect=True,
                        failed=True,
                    )
                    await _wait_or_stop(stop, _backoff_seconds(attempt, self._random))
        finally:
            await self._rest_client.aclose()
            if stop.is_set() or cancelled:
                await asyncio.to_thread(
                    self._repository.transition,
                    provider=PROVIDER,
                    feed=FEED,
                    state=MarketConnectionState.STOPPED,
                    now=self._clock.now(),
                )

    async def _stream_once(self, stop: asyncio.Event) -> None:
        queue: asyncio.Queue[StreamFrame | None] = asyncio.Queue(maxsize=2000)
        connected = asyncio.Event()
        consumer = asyncio.create_task(
            self._consume_frames(queue, connected),
            name="alpaca-iex-db-writer",
        )
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(connected, stop),
            name="alpaca-iex-heartbeat",
        )
        try:
            async for frame in self._stream_client.frames():
                if stop.is_set():
                    return
                if consumer.done():
                    await consumer
                await queue.put(frame)
        finally:
            try:
                if not consumer.done():
                    await queue.put(None)
                await consumer
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

    async def _consume_frames(
        self,
        queue: asyncio.Queue[StreamFrame | None],
        connected: asyncio.Event,
    ) -> None:
        while True:
            first = await queue.get()
            if first is None:
                return
            batch = [first]
            reached_end = False
            while len(batch) < 100 and not queue.empty():
                item = queue.get_nowait()
                if item is None:
                    reached_end = True
                    break
                batch.append(item)
            connected_frames = [frame for frame in batch if frame.connected]
            if connected_frames:
                await asyncio.to_thread(
                    self._repository.transition,
                    provider=PROVIDER,
                    feed=FEED,
                    state=MarketConnectionState.CONNECTED,
                    now=connected_frames[-1].received_at,
                )
                connected.set()
            data_frames = [frame for frame in batch if not frame.connected]
            if data_frames:
                await self._persist_frames(data_frames)
            if reached_end:
                return

    async def _heartbeat_loop(
        self,
        connected: asyncio.Event,
        stop: asyncio.Event,
    ) -> None:
        await connected.wait()
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self._heartbeat_interval_seconds,
                )
            except TimeoutError:
                await asyncio.to_thread(
                    self._repository.heartbeat,
                    provider=PROVIDER,
                    feed=FEED,
                    now=self._clock.now(),
                )

    async def _persist_frames(self, frames: list[StreamFrame]) -> None:
        bars: list[MarketBar] = []
        quotes: list[MarketQuote] = []
        trades: list[MarketTradeEvent] = []
        for frame in frames:
            if frame.raw_object_uri is None:
                raise AlpacaStreamError("PROTOCOL", "Data frame has no raw object URI")
            for message in frame.messages:
                event = parse_stream_message(
                    message,
                    available_at=frame.received_at,
                    raw_object_uri=frame.raw_object_uri,
                )
                if isinstance(event, MarketBar):
                    bars.append(event)
                elif isinstance(event, MarketQuote):
                    quotes.append(event)
                elif isinstance(event, MarketTradeEvent):
                    trades.append(event)
        await asyncio.to_thread(
            self._repository.append,
            bars=bars,
            quotes=quotes,
            trades=trades,
        )
        await asyncio.to_thread(
            self._repository.record_received,
            provider=PROVIDER,
            feed=FEED,
            received_at=max(frame.received_at for frame in frames),
            bar_at=_latest_time(bars),
            quote_at=_latest_time(quotes),
            trade_at=_latest_time(trades),
        )


def _latest_time(
    events: list[MarketBar] | list[MarketQuote] | list[MarketTradeEvent],
) -> datetime | None:
    return max((event.event_time for event in events), default=None)


def _is_auth_error(exc: AlpacaHttpError | AlpacaStreamError) -> bool:
    return exc.error_code in {"HTTP_401", "HTTP_403", "WS_401", "WS_402", "WS_403", "WS_404"}


def _backoff_seconds(attempt: int, random_source: random.Random) -> float:
    ceiling = min(60.0, 2.0 ** min(attempt, 6))
    return ceiling + random_source.uniform(0.0, min(1.0, ceiling * 0.2))


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
