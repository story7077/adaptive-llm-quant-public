from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, case, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import MarketBar, MarketQuote, MarketTradeEvent
from trading.domain.enums import MarketConnectionState, OrderSide
from trading.domain.time import require_aware_utc
from trading.persistence.models import (
    MarketBarRow,
    MarketQuoteRow,
    MarketStreamStatusRow,
    MarketTradeEventRow,
)


@dataclass(frozen=True, slots=True)
class IngestCounts:
    bars: int
    quotes: int
    trades: int

    @property
    def total(self) -> int:
        return self.bars + self.quotes + self.trades


class MarketDataRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def append(
        self,
        *,
        bars: list[MarketBar] | None = None,
        quotes: list[MarketQuote] | None = None,
        trades: list[MarketTradeEvent] | None = None,
    ) -> IngestCounts:
        bar_values = [_bar_values(item) for item in bars or ()]
        quote_values = [_quote_values(item) for item in quotes or ()]
        trade_values = [_trade_values(item) for item in trades or ()]
        with self._session_factory.begin() as session:
            bar_count = _insert_ignore(session, MarketBarRow, bar_values)
            quote_count = _insert_ignore(session, MarketQuoteRow, quote_values)
            trade_count = _insert_ignore(session, MarketTradeEventRow, trade_values)
        return IngestCounts(bars=bar_count, quotes=quote_count, trades=trade_count)

    def latest_bars(
        self,
        *,
        provider: str,
        feed: str,
        symbol: str,
        timeframe: str,
        as_of: datetime,
        limit: int,
    ) -> list[MarketBarRow]:
        cutoff = require_aware_utc(as_of, "as_of")
        ranked = (
            select(
                MarketBarRow.bar_id.label("bar_id"),
                func.row_number()
                .over(
                    partition_by=(
                        MarketBarRow.provider,
                        MarketBarRow.feed,
                        MarketBarRow.symbol,
                        MarketBarRow.timeframe,
                        MarketBarRow.event_time,
                    ),
                    order_by=(
                        MarketBarRow.available_at.desc(),
                        MarketBarRow.ingested_at.desc(),
                        case(
                            (
                                MarketBarRow.source_kind == "STREAM_UPDATE",
                                3,
                            ),
                            (
                                MarketBarRow.source_kind == "STREAM_BAR",
                                2,
                            ),
                            else_=1,
                        ).desc(),
                        MarketBarRow.bar_id.desc(),
                    ),
                )
                .label("revision_rank"),
            )
            .where(
                MarketBarRow.provider == provider,
                MarketBarRow.feed == feed,
                MarketBarRow.symbol == symbol,
                MarketBarRow.timeframe == timeframe,
                MarketBarRow.event_time <= cutoff,
                MarketBarRow.available_at <= cutoff,
            )
            .subquery()
        )
        statement = (
            select(MarketBarRow)
            .join(ranked, MarketBarRow.bar_id == ranked.c.bar_id)
            .where(ranked.c.revision_rank == 1)
            .order_by(MarketBarRow.event_time.desc())
            .limit(limit)
        )
        with self._session_factory() as session:
            rows = list(session.scalars(statement))
        rows.reverse()
        return rows

    def latest_quote(
        self,
        *,
        provider: str,
        feed: str,
        symbol: str,
        as_of: datetime,
    ) -> MarketQuoteRow | None:
        statement = _latest_statement(
            MarketQuoteRow,
            provider=provider,
            feed=feed,
            symbol=symbol,
            as_of=as_of,
        )
        with self._session_factory() as session:
            return session.scalars(statement).first()

    def latest_trade(
        self,
        *,
        provider: str,
        feed: str,
        symbol: str,
        as_of: datetime,
    ) -> MarketTradeEventRow | None:
        statement = _latest_statement(
            MarketTradeEventRow,
            provider=provider,
            feed=feed,
            symbol=symbol,
            as_of=as_of,
        ).where(MarketTradeEventRow.event_kind == "TRADE")
        with self._session_factory() as session:
            return session.scalars(statement).first()

    def first_quote_after(
        self,
        *,
        provider: str,
        feed: str,
        symbol: str,
        observed_after: datetime,
        as_of: datetime,
    ) -> MarketQuoteRow | None:
        start = require_aware_utc(observed_after, "observed_after")
        cutoff = require_aware_utc(as_of, "as_of")
        statement = (
            select(MarketQuoteRow)
            .where(
                MarketQuoteRow.provider == provider,
                MarketQuoteRow.feed == feed,
                MarketQuoteRow.symbol == symbol,
                MarketQuoteRow.event_time >= start,
                MarketQuoteRow.available_at >= start,
                MarketQuoteRow.available_at <= cutoff,
            )
            .order_by(
                MarketQuoteRow.available_at,
                MarketQuoteRow.event_time,
                MarketQuoteRow.quote_id,
            )
            .limit(1)
        )
        with self._session_factory() as session:
            return session.scalars(statement).first()

    def first_executable_quote(
        self,
        *,
        provider: str,
        feed: str,
        symbol: str,
        observed_after: datetime,
        as_of: datetime,
        max_age_seconds: int,
        side: OrderSide,
        max_spread_bps: Decimal | None = None,
    ) -> MarketQuoteRow | None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        start = require_aware_utc(observed_after, "observed_after")
        cutoff = require_aware_utc(as_of, "as_of")
        oldest_event = cutoff - timedelta(seconds=max_age_seconds)
        predicates = [
            MarketQuoteRow.provider == provider,
            MarketQuoteRow.feed == feed,
            MarketQuoteRow.symbol == symbol,
            MarketQuoteRow.event_time > start,
            MarketQuoteRow.event_time >= oldest_event,
            MarketQuoteRow.available_at > start,
            MarketQuoteRow.available_at <= cutoff,
            MarketQuoteRow.bid_price > 0,
            MarketQuoteRow.ask_price > 0,
            MarketQuoteRow.ask_price >= MarketQuoteRow.bid_price,
            (
                MarketQuoteRow.ask_size_round_lots
                if side is OrderSide.BUY
                else MarketQuoteRow.bid_size_round_lots
            )
            > 0,
        ]
        if max_spread_bps is not None:
            if max_spread_bps <= 0:
                raise ValueError("max_spread_bps must be positive")
            predicates.append(
                (
                    (MarketQuoteRow.ask_price - MarketQuoteRow.bid_price)
                    * Decimal("20000")
                    / (MarketQuoteRow.ask_price + MarketQuoteRow.bid_price)
                )
                <= max_spread_bps
            )
        statement = (
            select(MarketQuoteRow)
            .where(*predicates)
            .order_by(
                MarketQuoteRow.available_at,
                MarketQuoteRow.event_time,
                MarketQuoteRow.quote_id,
            )
            .limit(1)
        )
        with self._session_factory() as session:
            return session.scalars(statement).first()

    def last_bar_event_time(
        self,
        *,
        provider: str,
        feed: str,
        symbols: tuple[str, ...],
        timeframe: str,
    ) -> datetime | None:
        statement = (
            select(MarketBarRow.event_time)
            .where(
                MarketBarRow.provider == provider,
                MarketBarRow.feed == feed,
                MarketBarRow.symbol.in_(symbols),
                MarketBarRow.timeframe == timeframe,
            )
            .order_by(MarketBarRow.event_time.desc())
            .limit(1)
        )
        with self._session_factory() as session:
            value = session.scalar(statement)
        return None if value is None else _aware(value)

    def ensure_status(
        self,
        *,
        provider: str,
        feed: str,
        state: MarketConnectionState,
        now: datetime,
    ) -> None:
        instant = require_aware_utc(now, "now")
        with self._session_factory.begin() as session:
            row = session.get(MarketStreamStatusRow, (provider, feed))
            if row is None:
                session.add(
                    MarketStreamStatusRow(
                        provider=provider,
                        feed=feed,
                        state=state.value,
                        connected_at=None,
                        disconnected_at=None,
                        last_message_at=None,
                        last_bar_at=None,
                        last_quote_at=None,
                        last_trade_at=None,
                        reconnect_count=0,
                        consecutive_failures=0,
                        last_error_code=None,
                        last_error_detail=None,
                        updated_at=instant,
                    )
                )

    def transition(
        self,
        *,
        provider: str,
        feed: str,
        state: MarketConnectionState,
        now: datetime,
        error_code: str | None = None,
        error_detail: str | None = None,
        increment_reconnect: bool = False,
        failed: bool = False,
    ) -> None:
        instant = require_aware_utc(now, "now")
        with self._session_factory.begin() as session:
            row = _status_row(session, provider, feed, instant)
            row.state = state.value
            row.updated_at = instant
            row.last_error_code = error_code
            row.last_error_detail = None if error_detail is None else error_detail[:500]
            if state is MarketConnectionState.CONNECTED:
                row.connected_at = instant
                row.consecutive_failures = 0
            elif state in {
                MarketConnectionState.RECONNECTING,
                MarketConnectionState.STOPPED,
                MarketConnectionState.AUTH_REQUIRED,
            }:
                row.disconnected_at = instant
            if increment_reconnect:
                row.reconnect_count += 1
            if failed:
                row.consecutive_failures += 1

    def record_received(
        self,
        *,
        provider: str,
        feed: str,
        received_at: datetime,
        bar_at: datetime | None = None,
        quote_at: datetime | None = None,
        trade_at: datetime | None = None,
    ) -> None:
        instant = require_aware_utc(received_at, "received_at")
        with self._session_factory.begin() as session:
            row = _status_row(session, provider, feed, instant)
            row.last_message_at = instant
            row.updated_at = instant
            if bar_at is not None:
                row.last_bar_at = max(_aware_or_min(row.last_bar_at), require_aware_utc(bar_at))
            if quote_at is not None:
                row.last_quote_at = max(
                    _aware_or_min(row.last_quote_at),
                    require_aware_utc(quote_at),
                )
            if trade_at is not None:
                row.last_trade_at = max(
                    _aware_or_min(row.last_trade_at),
                    require_aware_utc(trade_at),
                )

    def heartbeat(
        self,
        *,
        provider: str,
        feed: str,
        now: datetime,
    ) -> None:
        instant = require_aware_utc(now, "now")
        with self._session_factory.begin() as session:
            row = session.get(MarketStreamStatusRow, (provider, feed))
            if row is not None and row.state == MarketConnectionState.CONNECTED.value:
                row.updated_at = instant

    def status(self, *, provider: str, feed: str) -> MarketStreamStatusRow | None:
        with self._session_factory() as session:
            return session.get(MarketStreamStatusRow, (provider, feed))


def _insert_ignore(
    session: Session,
    model: type[MarketBarRow] | type[MarketQuoteRow] | type[MarketTradeEventRow],
    values: list[dict[str, Any]],
) -> int:
    if not values:
        return 0
    dialect = session.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        before = session.query(model).count()
        for value in values:
            session.merge(model(**value))
        session.flush()
        return session.query(model).count() - before

    inserted = 0
    # PostgreSQL's extended-query protocol caps a statement at 65,535
    # parameters. Daily multi-symbol history can exceed that even when the
    # provider response itself is modest, so keep each idempotent insert bounded.
    for offset in range(0, len(values), 500):
        batch = values[offset : offset + 500]
        statement = (
            postgresql_insert(model).values(batch).on_conflict_do_nothing()
            if dialect == "postgresql"
            else sqlite_insert(model).values(batch).on_conflict_do_nothing()
        )
        primary_key = (
            MarketBarRow.bar_id
            if model is MarketBarRow
            else (
                MarketQuoteRow.quote_id
                if model is MarketQuoteRow
                else MarketTradeEventRow.trade_event_id
            )
        )
        result = session.execute(statement.returning(primary_key))
        inserted += len(result.scalars().all())
    return inserted


def _latest_statement(
    model: type[MarketQuoteRow] | type[MarketTradeEventRow],
    *,
    provider: str,
    feed: str,
    symbol: str,
    as_of: datetime,
) -> Select[tuple[Any]]:
    cutoff = require_aware_utc(as_of, "as_of")
    id_column = (
        MarketQuoteRow.quote_id
        if model is MarketQuoteRow
        else MarketTradeEventRow.trade_event_id
    )
    return (
        select(model)
        .where(
            model.provider == provider,
            model.feed == feed,
            model.symbol == symbol,
            model.event_time <= cutoff,
            model.available_at <= cutoff,
        )
        .order_by(
            model.event_time.desc(),
            model.available_at.desc(),
            model.ingested_at.desc(),
            id_column.desc(),
        )
        .limit(1)
    )


def _status_row(
    session: Session,
    provider: str,
    feed: str,
    now: datetime,
) -> MarketStreamStatusRow:
    row = session.get(MarketStreamStatusRow, (provider, feed))
    if row is not None:
        return row
    row = MarketStreamStatusRow(
        provider=provider,
        feed=feed,
        state=MarketConnectionState.STOPPED.value,
        connected_at=None,
        disconnected_at=None,
        last_message_at=None,
        last_bar_at=None,
        last_quote_at=None,
        last_trade_at=None,
        reconnect_count=0,
        consecutive_failures=0,
        last_error_code=None,
        last_error_detail=None,
        updated_at=now,
    )
    session.add(row)
    return row


def _bar_values(item: MarketBar) -> dict[str, Any]:
    return {
        "bar_id": item.bar_id,
        "provider": item.provider,
        "feed": item.feed,
        "symbol": item.symbol,
        "timeframe": item.timeframe,
        "event_time": item.event_time,
        "provider_timestamp": item.provider_timestamp,
        "available_at": item.available_at,
        "ingested_at": item.ingested_at,
        "source_kind": item.source_kind.value,
        "open": item.open,
        "high": item.high,
        "low": item.low,
        "close": item.close,
        "volume": item.volume,
        "vwap": item.vwap,
        "trade_count": item.trade_count,
        "request_id": item.request_id,
        "payload_hash": item.payload_hash,
        "raw_object_uri": item.raw_object_uri,
        "payload_json": item.payload,
    }


def _quote_values(item: MarketQuote) -> dict[str, Any]:
    return {
        "quote_id": item.quote_id,
        "provider": item.provider,
        "feed": item.feed,
        "symbol": item.symbol,
        "event_time": item.event_time,
        "provider_timestamp": item.provider_timestamp,
        "available_at": item.available_at,
        "ingested_at": item.ingested_at,
        "source_kind": item.source_kind.value,
        "bid_exchange": item.bid_exchange,
        "bid_price": item.bid_price,
        "bid_size_round_lots": item.bid_size_round_lots,
        "ask_exchange": item.ask_exchange,
        "ask_price": item.ask_price,
        "ask_size_round_lots": item.ask_size_round_lots,
        "conditions": item.conditions,
        "tape": item.tape,
        "payload_hash": item.payload_hash,
        "raw_object_uri": item.raw_object_uri,
        "payload_json": item.payload,
    }


def _trade_values(item: MarketTradeEvent) -> dict[str, Any]:
    return {
        "trade_event_id": item.trade_event_id,
        "provider": item.provider,
        "feed": item.feed,
        "symbol": item.symbol,
        "event_kind": item.event_kind.value,
        "provider_event_id": item.provider_event_id,
        "event_time": item.event_time,
        "provider_timestamp": item.provider_timestamp,
        "available_at": item.available_at,
        "ingested_at": item.ingested_at,
        "source_kind": item.source_kind.value,
        "exchange": item.exchange,
        "price": item.price,
        "size": item.size,
        "conditions": item.conditions,
        "tape": item.tape,
        "payload_hash": item.payload_hash,
        "raw_object_uri": item.raw_object_uri,
        "payload_json": item.payload,
    }


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _aware_or_min(value: datetime | None) -> datetime:
    return datetime.min.replace(tzinfo=UTC) if value is None else _aware(value)
