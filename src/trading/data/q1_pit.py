from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import FEED, PROVIDER
from trading.data.market_repository import MarketDataRepository
from trading.domain.time import require_aware_utc
from trading.persistence.models import MarketBarRow

NEW_YORK = ZoneInfo("America/New_York")


class Q1PointInTimeDataError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CompletedDailySeries:
    symbol: str
    session_dates: tuple[date, ...]
    adjusted_closes: tuple[Decimal, ...]
    volumes: tuple[Decimal, ...]
    bar_ids: tuple[str, ...]
    available_ats: tuple[datetime, ...]

    def __post_init__(self) -> None:
        lengths = {
            len(self.session_dates),
            len(self.adjusted_closes),
            len(self.volumes),
            len(self.bar_ids),
            len(self.available_ats),
        }
        if len(lengths) != 1:
            raise ValueError("Completed daily series fields must be aligned")
        if any(value <= 0 for value in self.adjusted_closes):
            raise ValueError("Completed daily closes must be positive")
        if any(value < 0 for value in self.volumes):
            raise ValueError("Completed daily volumes cannot be negative")
        if len(set(self.session_dates)) != len(self.session_dates):
            raise ValueError("Completed daily series contains duplicate sessions")


@dataclass(frozen=True, slots=True)
class AlignedDailyInputs:
    session_dates: tuple[date, ...]
    series: dict[str, CompletedDailySeries]
    source_bar_ids: tuple[str, ...]
    signal_data_cutoff: datetime


class Q1PointInTimeMarketData:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._repository = MarketDataRepository(session_factory)

    def aligned_completed_daily_inputs(
        self,
        *,
        symbols: tuple[str, ...],
        current_session_date: date,
        expected_latest_completed_session: date,
        signal_data_cutoff: datetime,
        minimum_completed_sessions: int,
        query_limit: int,
        dataset_version: str,
    ) -> AlignedDailyInputs:
        if not symbols:
            raise ValueError("Q1 daily inputs require symbols")
        if minimum_completed_sessions <= 0:
            raise ValueError("minimum_completed_sessions must be positive")
        if query_limit < minimum_completed_sessions:
            raise ValueError("query_limit is smaller than the required history")
        cutoff = require_aware_utc(signal_data_cutoff, "signal_data_cutoff")
        loaded = {
            symbol: self._completed_series(
                symbol=symbol,
                current_session_date=current_session_date,
                cutoff=cutoff,
                limit=query_limit,
                dataset_version=dataset_version,
            )
            for symbol in symbols
        }
        date_sets: dict[str, set[date]] = {
            symbol: set(item.session_dates)
            for symbol, item in loaded.items()
        }
        common_dates = set(next(iter(date_sets.values())))
        for dates in tuple(date_sets.values())[1:]:
            common_dates.intersection_update(dates)
        if len(common_dates) < minimum_completed_sessions:
            raise Q1PointInTimeDataError(
                "Insufficient aligned completed sessions for Q1 signal"
            )
        ordered_common = tuple(sorted(common_dates))
        if ordered_common[-1] != expected_latest_completed_session:
            raise Q1PointInTimeDataError(
                "Q1 completed daily history is stale for the market calendar"
            )
        selected_dates = ordered_common[-minimum_completed_sessions:]
        selected_date_set = set(selected_dates)
        for symbol, dates in date_sets.items():
            recent_dates = {
                value
                for value in dates
                if value >= selected_dates[0]
            }
            if recent_dates != selected_date_set:
                raise Q1PointInTimeDataError(
                    f"Incomplete or inconsistent recent daily history for {symbol}"
                )
        aligned = {
            symbol: _select_dates(item, selected_date_set)
            for symbol, item in loaded.items()
        }
        return AlignedDailyInputs(
            session_dates=selected_dates,
            series=aligned,
            source_bar_ids=tuple(
                sorted(
                    bar_id
                    for item in aligned.values()
                    for bar_id in item.bar_ids
                )
            ),
            signal_data_cutoff=cutoff,
        )

    def completed_adv_shares(
        self,
        *,
        symbol: str,
        current_session_date: date,
        as_of: datetime,
        lookback_sessions: int,
        query_limit: int,
        dataset_version: str,
    ) -> tuple[Decimal, tuple[str, ...]]:
        if lookback_sessions <= 0 or query_limit < lookback_sessions:
            raise ValueError("Invalid Q1 ADV lookback")
        series = self._completed_series(
            symbol=symbol,
            current_session_date=current_session_date,
            cutoff=require_aware_utc(as_of, "as_of"),
            limit=query_limit,
            dataset_version=dataset_version,
        )
        if len(series.volumes) < lookback_sessions:
            raise Q1PointInTimeDataError(
                f"Insufficient completed ADV history for {symbol}"
            )
        volumes = series.volumes[-lookback_sessions:]
        average = sum(volumes, Decimal("0")) / Decimal(lookback_sessions)
        if average <= 0:
            raise Q1PointInTimeDataError(f"Non-positive completed ADV for {symbol}")
        return average, series.bar_ids[-lookback_sessions:]

    def _completed_series(
        self,
        *,
        symbol: str,
        current_session_date: date,
        cutoff: datetime,
        limit: int,
        dataset_version: str,
    ) -> CompletedDailySeries:
        rows = self._repository.latest_bars(
            provider=PROVIDER,
            feed=FEED,
            symbol=symbol,
            timeframe="1Day",
            as_of=cutoff,
            limit=limit,
        )
        by_date: dict[date, MarketBarRow] = {}
        for row in rows:
            session_date = _aware(row.event_time).astimezone(NEW_YORK).date()
            if session_date >= current_session_date:
                continue
            available_at = _aware(row.available_at)
            if available_at > cutoff:
                raise Q1PointInTimeDataError(
                    f"Daily bar {row.bar_id} was unavailable at the cutoff"
                )
            if (
                row.payload_json.get("_adjustment") != "all"
                or row.payload_json.get("_dataset_version") != dataset_version
            ):
                raise Q1PointInTimeDataError(
                    f"Daily bar {row.bar_id} lacks required adjusted provenance"
                )
            previous = by_date.get(session_date)
            if previous is not None and (
                previous.close != row.close
                or previous.volume != row.volume
                or previous.payload_hash != row.payload_hash
            ):
                raise Q1PointInTimeDataError(
                    f"Inconsistent duplicate daily bars for {symbol} {session_date}"
                )
            by_date[session_date] = row
        ordered = [by_date[value] for value in sorted(by_date)]
        if not ordered:
            raise Q1PointInTimeDataError(
                f"No completed adjusted daily history for {symbol}"
            )
        if any(row.close <= 0 or row.volume < 0 for row in ordered):
            raise Q1PointInTimeDataError(
                f"Invalid completed daily values for {symbol}"
            )
        return CompletedDailySeries(
            symbol=symbol,
            session_dates=tuple(
                _aware(row.event_time).astimezone(NEW_YORK).date()
                for row in ordered
            ),
            adjusted_closes=tuple(row.close for row in ordered),
            volumes=tuple(row.volume for row in ordered),
            bar_ids=tuple(row.bar_id for row in ordered),
            available_ats=tuple(_aware(row.available_at) for row in ordered),
        )


def _select_dates(
    series: CompletedDailySeries,
    selected: set[date],
) -> CompletedDailySeries:
    indexes = [
        index
        for index, value in enumerate(series.session_dates)
        if value in selected
    ]
    return CompletedDailySeries(
        symbol=series.symbol,
        session_dates=tuple(series.session_dates[index] for index in indexes),
        adjusted_closes=tuple(
            series.adjusted_closes[index]
            for index in indexes
        ),
        volumes=tuple(series.volumes[index] for index in indexes),
        bar_ids=tuple(series.bar_ids[index] for index in indexes),
        available_ats=tuple(series.available_ats[index] for index in indexes),
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
