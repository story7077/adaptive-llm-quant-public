from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from trading.data.alpaca import FEED, PROVIDER, AlpacaRestClient
from trading.data.market_repository import MarketDataRepository
from trading.domain.time import Clock, SystemClock


@dataclass(frozen=True, slots=True)
class HistoryBackfillResult:
    timeframe: str
    start: datetime
    end: datetime
    fetched: int
    inserted: int


class MarketHistoryService:
    def __init__(
        self,
        *,
        repository: MarketDataRepository,
        client: AlpacaRestClient,
        symbols: tuple[str, ...],
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._client = client
        self._symbols = symbols
        self._clock = clock or SystemClock()

    async def backfill_daily(self, *, lookback_days: int = 500) -> HistoryBackfillResult:
        if lookback_days < 100:
            raise ValueError("Daily history needs at least 100 calendar days")
        end = self._clock.now()
        latest = self._repository.last_bar_event_time(
            provider=PROVIDER,
            feed=FEED,
            symbols=self._symbols,
            timeframe="1Day",
        )
        provenance_ready = all(
            (
                rows := self._repository.latest_bars(
                    provider=PROVIDER,
                    feed=FEED,
                    symbol=symbol,
                    timeframe="1Day",
                    as_of=end,
                    limit=1,
                )
            )
            and rows[-1].payload_json.get("_adjustment") == "all"
            and rows[-1].payload_json.get("_dataset_version")
            == "alpaca_iex_adjusted_all_v1"
            for symbol in self._symbols
        )
        if not provenance_ready:
            latest = None
        desired_start = end - timedelta(days=lookback_days)
        start = (
            desired_start
            if latest is None
            else max(latest - timedelta(days=90), desired_start)
        )
        bars = await self._client.fetch_bars(
            symbols=self._symbols,
            start=start,
            end=end,
            timeframe="1Day",
            adjustment="all",
        )
        counts = self._repository.append(bars=bars)
        return HistoryBackfillResult(
            timeframe="1Day",
            start=start,
            end=end,
            fetched=len(bars),
            inserted=counts.bars,
        )
