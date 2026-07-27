from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import FEED, PROVIDER
from trading.data.market_repository import MarketDataRepository
from trading.data.universe import market_data_symbols
from trading.domain.enums import MarketConnectionState
from trading.domain.time import Clock, SystemClock, require_aware_utc
from trading.persistence.models import MarketBarRow, MarketQuoteRow, MarketTradeEventRow
from trading.settings import ConfigBundle, Settings


class LiveMarketError(ValueError):
    pass


class LiveMarketSnapshotService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        config: ConfigBundle,
        clock: Clock | None = None,
    ) -> None:
        self._repository = MarketDataRepository(session_factory)
        self._settings = settings
        self._symbols = market_data_symbols(config)
        self._clock = clock or SystemClock()

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def snapshot(
        self,
        *,
        symbol: str | None = None,
        timeframe: str = "1Min",
        limit: int = 120,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        if timeframe != "1Min":
            raise LiveMarketError("Only timeframe=1Min is supported")
        if not 1 <= limit <= 500:
            raise LiveMarketError("limit must be between 1 and 500")
        selected = (symbol or self._symbols[0]).upper()
        if selected not in self._symbols:
            raise LiveMarketError(f"Unknown market-data symbol: {selected}")
        now = require_aware_utc(as_of or self._clock.now(), "as_of")
        bars = self._repository.latest_bars(
            provider=PROVIDER,
            feed=FEED,
            symbol=selected,
            timeframe=timeframe,
            as_of=now,
            limit=limit,
        )
        quote = self._repository.latest_quote(
            provider=PROVIDER,
            feed=FEED,
            symbol=selected,
            as_of=now,
        )
        trade = self._repository.latest_trade(
            provider=PROVIDER,
            feed=FEED,
            symbol=selected,
            as_of=now,
        )
        status = self._repository.status(provider=PROVIDER, feed=FEED)

        quote_age = _age_seconds(now, None if quote is None else quote.event_time)
        bar_age = _age_seconds(now, None if not bars else bars[-1].event_time)
        quote_stale = quote_age is None or quote_age > self._settings.market_quote_stale_seconds
        bar_stale = bar_age is None or bar_age > self._settings.market_bar_stale_seconds
        connection_state = _connection_state(status, self._settings, now)
        data_status = _data_status(
            enabled=self._settings.market_data_enabled,
            configured=self._settings.has_alpaca_credentials,
            quote=quote,
            bars=bars,
            quote_stale=quote_stale,
            bar_stale=bar_stale,
        )
        paper_reason = _paper_input_reason(quote, quote_stale)
        last_price, price_basis = _last_price(trade, quote)
        change, change_pct = _bar_change(bars)
        return {
            "server_time": now.isoformat().replace("+00:00", "Z"),
            "source": {
                "mode": "LIVE_SHADOW",
                "provider": PROVIDER,
                "feed": FEED,
                "coverage": "SINGLE_EXCHANGE",
                "configured": self._settings.has_alpaca_credentials,
                "enabled": self._settings.market_data_enabled,
                "connection_state": connection_state,
                "data_status": data_status,
                "last_event_at": _latest_event_at(bars, quote, trade),
                "last_received_at": _iso(None if status is None else status.last_message_at),
                "heartbeat_age_seconds": _age_seconds(
                    now,
                    None if status is None else status.updated_at,
                ),
                "quote_age_seconds": quote_age,
                "bar_age_seconds": bar_age,
                "quote_stale": quote_stale,
                "bar_stale": bar_stale,
                "quote_stale_after_seconds": self._settings.market_quote_stale_seconds,
                "bar_stale_after_seconds": self._settings.market_bar_stale_seconds,
                "last_error_code": None if status is None else status.last_error_code,
                "last_error_detail": None if status is None else status.last_error_detail,
                "reconnect_count": 0 if status is None else status.reconnect_count,
                "poll_after_ms": self._settings.market_poll_after_ms,
                "candle_quality": "NATIVE_OHLCV",
                "candle_note": (
                    "Alpaca 무료 IEX 단일 거래소 데이터입니다. "
                    "미국 통합시세(SIP/NBBO)가 아닙니다."
                ),
            },
            "filters": {
                "symbols": list(self._symbols),
                "selected_symbol": selected,
                "timeframes": ["1Min"],
                "selected_timeframe": timeframe,
            },
            "market": {
                "quote": {
                    "price": _decimal_string(last_price),
                    "price_basis": price_basis,
                    "bid": _decimal_string(None if quote is None else quote.bid_price),
                    "ask": _decimal_string(None if quote is None else quote.ask_price),
                    "bid_size_round_lots": (
                        None if quote is None else quote.bid_size_round_lots
                    ),
                    "ask_size_round_lots": (
                        None if quote is None else quote.ask_size_round_lots
                    ),
                    "bid_size_shares": (
                        None if quote is None else quote.bid_size_round_lots * 100
                    ),
                    "ask_size_shares": (
                        None if quote is None else quote.ask_size_round_lots * 100
                    ),
                    "event_time": _iso(None if quote is None else quote.event_time),
                    "trade_event_time": _iso(
                        None if trade is None else trade.event_time
                    ),
                    "change": _decimal_string(change),
                    "change_pct": _decimal_string(change_pct),
                },
                "candles": [_candle(item) for item in bars],
                "empty_reason": None if bars else "NO_STORED_BARS",
            },
            "paper_input": {
                "ready": paper_reason is None,
                "reason": paper_reason,
                "quote_id": None if quote is None else quote.quote_id,
                "pricing_rule": "BUY_ASK_SELL_BID_PLUS_DELAY_V1",
                "real_order_routing": False,
            },
        }


def _connection_state(status: Any, settings: Settings, now: datetime) -> str:
    if not settings.market_data_enabled:
        return MarketConnectionState.STOPPED.value
    if not settings.has_alpaca_credentials:
        return MarketConnectionState.AUTH_REQUIRED.value
    if status is None:
        return MarketConnectionState.STOPPED.value
    heartbeat_age = _age_seconds(now, status.updated_at)
    if (
        status.state == MarketConnectionState.CONNECTED.value
        and heartbeat_age is not None
        and heartbeat_age > settings.market_connection_stale_seconds
    ):
        return MarketConnectionState.DISCONNECTED.value
    return str(status.state)


def _data_status(
    *,
    enabled: bool,
    configured: bool,
    quote: MarketQuoteRow | None,
    bars: list[MarketBarRow],
    quote_stale: bool,
    bar_stale: bool,
) -> str:
    if not enabled:
        return "STOPPED"
    if not configured:
        return "AUTH_REQUIRED"
    if quote is None and not bars:
        return "NO_DATA"
    if quote_stale or bar_stale:
        return "STALE"
    return "LIVE"


def _paper_input_reason(quote: MarketQuoteRow | None, stale: bool) -> str | None:
    if quote is None:
        return "NO_QUOTE"
    if stale:
        return "STALE_QUOTE"
    if quote.bid_price <= 0 or quote.ask_price <= 0:
        return "NON_POSITIVE_QUOTE"
    if quote.ask_price < quote.bid_price:
        return "CROSSED_QUOTE"
    return None


def _last_price(
    trade: MarketTradeEventRow | None,
    quote: MarketQuoteRow | None,
) -> tuple[Decimal | None, str | None]:
    if trade is not None and trade.price is not None:
        return trade.price, "IEX_LAST_TRADE"
    if quote is not None and quote.bid_price > 0 and quote.ask_price > 0:
        return (quote.bid_price + quote.ask_price) / Decimal("2"), "IEX_MIDPOINT"
    return None, None


def _bar_change(
    bars: list[MarketBarRow],
) -> tuple[Decimal | None, Decimal | None]:
    if len(bars) < 2:
        return None, None
    previous = bars[-2].close
    latest = bars[-1].close
    change = latest - previous
    return change, None if previous == 0 else change / previous * Decimal("100")


def _candle(row: MarketBarRow) -> dict[str, Any]:
    return {
        "bar_id": row.bar_id,
        "time": _iso(row.event_time),
        "open": _decimal_string(row.open),
        "high": _decimal_string(row.high),
        "low": _decimal_string(row.low),
        "close": _decimal_string(row.close),
        "volume": _decimal_string(row.volume),
        "vwap": _decimal_string(row.vwap),
        "trade_count": row.trade_count,
        "source_kind": row.source_kind,
        "high_low_derived": False,
    }


def _latest_event_at(
    bars: list[MarketBarRow],
    quote: MarketQuoteRow | None,
    trade: MarketTradeEventRow | None,
) -> str | None:
    times = [
        _aware(value)
        for value in (
            None if not bars else bars[-1].event_time,
            None if quote is None else quote.event_time,
            None if trade is None else trade.event_time,
        )
        if value is not None
    ]
    return None if not times else _iso(max(times))


def _age_seconds(now: datetime, value: datetime | None) -> float | None:
    if value is None:
        return None
    return round(max(0.0, (now - _aware(value)).total_seconds()), 3)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware(value).isoformat().replace("+00:00", "Z")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _decimal_string(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
