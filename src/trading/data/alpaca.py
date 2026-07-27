from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, cast

import httpx
import websockets
from pydantic import JsonValue

from trading.data.raw_store import ImmutableRawStore
from trading.domain.contracts import MarketBar, MarketQuote, MarketTradeEvent
from trading.domain.enums import MarketDataSourceKind, MarketTradeEventKind
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import Clock, SystemClock, require_aware_utc

PROVIDER = "alpaca"
FEED = "iex"
TIMEFRAME = "1Min"
SUPPORTED_TIMEFRAMES = frozenset({"1Min", "1Hour", "1Day"})

type MarketEvent = MarketBar | MarketQuote | MarketTradeEvent
type JsonObject = dict[str, object]


@dataclass(frozen=True, slots=True, repr=False)
class AlpacaCredentials:
    key_id: str
    secret_key: str

    def __post_init__(self) -> None:
        if not self.key_id.strip() or not self.secret_key.strip():
            raise ValueError("Alpaca key ID and secret key are required")

    @property
    def headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
        }


class AlpacaHttpError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.error_code = f"HTTP_{status_code}"
        super().__init__(f"Alpaca market-data request failed ({status_code}): {detail[:240]}")


class AlpacaStreamError(RuntimeError):
    def __init__(self, code: int | str, detail: str) -> None:
        self.error_code = f"WS_{code}"
        super().__init__(f"Alpaca market-data stream failed ({code}): {detail[:240]}")


@dataclass(frozen=True, slots=True)
class StreamFrame:
    received_at: datetime
    raw_object_uri: str | None
    messages: list[JsonObject]
    connected: bool = False


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


type ConnectionFactory = Callable[
    [str],
    AbstractAsyncContextManager[WebSocketLike],
]


class AlpacaRestClient:
    def __init__(
        self,
        *,
        credentials: AlpacaCredentials,
        raw_store: ImmutableRawStore,
        base_url: str = "https://data.alpaca.markets",
        clock: Clock | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._credentials = credentials
        self._raw_store = raw_store
        self._base_url = base_url.rstrip("/")
        self._clock = clock or SystemClock()
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_bars(
        self,
        *,
        symbols: tuple[str, ...],
        start: datetime,
        end: datetime,
        limit: int = 10_000,
        timeframe: str = TIMEFRAME,
        adjustment: str = "raw",
    ) -> list[MarketBar]:
        if not symbols:
            return []
        start_at = require_aware_utc(start, "start")
        end_at = require_aware_utc(end, "end")
        if start_at >= end_at:
            raise ValueError("Bar backfill start must precede end")
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"Unsupported Alpaca timeframe: {timeframe}")
        if adjustment not in {"raw", "split", "dividend", "all"}:
            raise ValueError(f"Unsupported Alpaca adjustment: {adjustment}")
        params: dict[str, str | int] = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": _rfc3339(start_at),
            "end": _rfc3339(end_at),
            "feed": FEED,
            "adjustment": adjustment,
            "sort": "asc",
            "limit": max(1, min(limit, 10_000)),
        }
        bars: list[MarketBar] = []
        seen_tokens: set[str] = set()
        while True:
            payload, available_at, raw_uri, request_id = await self._get(
                "/v2/stocks/bars",
                params=params,
                channel="rest-bars",
            )
            raw_by_symbol = payload.get("bars", {})
            if not isinstance(raw_by_symbol, dict):
                raise AlpacaHttpError(200, "Malformed bars payload")
            by_symbol = cast(dict[str, object], raw_by_symbol)
            for symbol, items in by_symbol.items():
                if not isinstance(items, list):
                    raise AlpacaHttpError(200, "Malformed bars payload")
                bar_items = cast(list[object], items)
                bars.extend(
                    parse_rest_bar(
                        symbol=symbol,
                        payload=_object(item),
                        available_at=available_at,
                        raw_object_uri=raw_uri,
                        request_id=request_id,
                        timeframe=timeframe,
                        adjustment=adjustment,
                    )
                    for item in bar_items
                )
            token = payload.get("next_page_token")
            if token is None:
                return bars
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise AlpacaHttpError(200, "Invalid or repeated next_page_token")
            seen_tokens.add(token)
            params["page_token"] = token

    async def fetch_latest_quotes(
        self,
        *,
        symbols: tuple[str, ...],
    ) -> list[MarketQuote]:
        if not symbols:
            return []
        payload, available_at, raw_uri, _ = await self._get(
            "/v2/stocks/quotes/latest",
            params={"symbols": ",".join(symbols), "feed": FEED},
            channel="rest-latest-quotes",
        )
        raw_by_symbol = payload.get("quotes", {})
        if not isinstance(raw_by_symbol, dict):
            raise AlpacaHttpError(200, "Malformed latest-quotes payload")
        by_symbol = cast(dict[str, object], raw_by_symbol)
        return [
            parse_quote(
                _object(item),
                symbol=str(symbol),
                available_at=available_at,
                raw_object_uri=raw_uri,
                source_kind=MarketDataSourceKind.REST_LATEST,
            )
            for symbol, item in by_symbol.items()
            if item is not None
        ]

    async def fetch_latest_trades(
        self,
        *,
        symbols: tuple[str, ...],
    ) -> list[MarketTradeEvent]:
        if not symbols:
            return []
        payload, available_at, raw_uri, _ = await self._get(
            "/v2/stocks/trades/latest",
            params={"symbols": ",".join(symbols), "feed": FEED},
            channel="rest-latest-trades",
        )
        raw_by_symbol = payload.get("trades", {})
        if not isinstance(raw_by_symbol, dict):
            raise AlpacaHttpError(200, "Malformed latest-trades payload")
        by_symbol = cast(dict[str, object], raw_by_symbol)
        return [
            parse_trade(
                _object(item),
                symbol=str(symbol),
                available_at=available_at,
                raw_object_uri=raw_uri,
                source_kind=MarketDataSourceKind.REST_LATEST,
            )
            for symbol, item in by_symbol.items()
            if item is not None
        ]

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int],
        channel: str,
    ) -> tuple[JsonObject, datetime, str, str | None]:
        response = await self._client.get(
            f"{self._base_url}{path}",
            params=params,
            headers=self._credentials.headers,
        )
        available_at = self._clock.now()
        raw_uri = self._raw_store.persist(
            provider=PROVIDER,
            feed=FEED,
            channel=channel,
            received_at=available_at,
            content=response.content,
        )
        if response.status_code >= 400:
            raise AlpacaHttpError(response.status_code, _safe_http_detail(response.text))
        try:
            decoded: object = json.loads(response.text, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise AlpacaHttpError(response.status_code, "Invalid JSON response") from exc
        request_id = response.headers.get("x-request-id")
        return _object(decoded), available_at, raw_uri, request_id


class AlpacaStreamClient:
    def __init__(
        self,
        *,
        credentials: AlpacaCredentials,
        symbols: tuple[str, ...],
        raw_store: ImmutableRawStore,
        trade_symbols: tuple[str, ...] | None = None,
        quote_symbols: tuple[str, ...] | None = None,
        bar_symbols: tuple[str, ...] | None = None,
        updated_bar_symbols: tuple[str, ...] | None = None,
        url: str = "wss://stream.data.alpaca.markets/v2/iex",
        clock: Clock | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if not symbols:
            raise ValueError("At least one stream symbol is required")
        self._credentials = credentials
        self._symbols = symbols
        self._channel_symbols = {
            "trades": symbols if trade_symbols is None else trade_symbols,
            "quotes": symbols if quote_symbols is None else quote_symbols,
            "bars": symbols if bar_symbols is None else bar_symbols,
            "updatedBars": (
                symbols if updated_bar_symbols is None else updated_bar_symbols
            ),
        }
        allowed = set(symbols)
        for channel, channel_symbols in self._channel_symbols.items():
            if not set(channel_symbols).issubset(allowed):
                raise ValueError(f"{channel} subscriptions must be in symbols")
        if not any(self._channel_symbols.values()):
            raise ValueError("At least one stream subscription is required")
        if sum(len(items) for items in self._channel_symbols.values()) > 30:
            raise ValueError("Alpaca Basic IEX supports at most 30 subscriptions")
        self._raw_store = raw_store
        self._url = url
        self._clock = clock or SystemClock()
        self._connection_factory = connection_factory or _default_connection_factory

    async def frames(self) -> AsyncIterator[StreamFrame]:
        async with self._connection_factory(self._url) as socket:
            connected = await _receive_messages(socket, timeout_seconds=10)
            _expect_success(connected, "connected")
            await socket.send(
                json.dumps(
                    {
                        "action": "auth",
                        "key": self._credentials.key_id,
                        "secret": self._credentials.secret_key,
                    },
                    separators=(",", ":"),
                )
            )
            authenticated = await _receive_messages(socket, timeout_seconds=10)
            _expect_success(authenticated, "authenticated")
            await socket.send(
                json.dumps(
                    {
                        "action": "subscribe",
                        **self._channel_symbols,
                    },
                    separators=(",", ":"),
                )
            )
            subscribed = await _receive_messages(socket, timeout_seconds=10)
            _expect_subscription(subscribed, self._channel_symbols)
            yield StreamFrame(
                received_at=self._clock.now(),
                raw_object_uri=None,
                messages=[],
                connected=True,
            )

            while True:
                raw = await socket.recv()
                received_at = self._clock.now()
                raw_text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                messages = _decode_message_array(raw_text)
                _raise_stream_error(messages)
                raw_uri = self._raw_store.persist(
                    provider=PROVIDER,
                    feed=FEED,
                    channel="stream",
                    received_at=received_at,
                    content=raw_text,
                )
                yield StreamFrame(
                    received_at=received_at,
                    raw_object_uri=raw_uri,
                    messages=messages,
                )


def parse_stream_message(
    payload: JsonObject,
    *,
    available_at: datetime,
    raw_object_uri: str,
) -> MarketEvent | None:
    message_type = payload.get("T")
    if message_type in {"b", "u"}:
        return parse_bar(
            payload,
            available_at=available_at,
            raw_object_uri=raw_object_uri,
            source_kind=(
                MarketDataSourceKind.STREAM_UPDATE
                if message_type == "u"
                else MarketDataSourceKind.STREAM_BAR
            ),
        )
    if message_type == "q":
        return parse_quote(
            payload,
            symbol=_required_string(payload, "S"),
            available_at=available_at,
            raw_object_uri=raw_object_uri,
            source_kind=MarketDataSourceKind.STREAM_QUOTE,
        )
    if message_type in {"t", "c", "x"}:
        return parse_trade(
            payload,
            symbol=_required_string(payload, "S"),
            available_at=available_at,
            raw_object_uri=raw_object_uri,
            source_kind=MarketDataSourceKind.STREAM_TRADE,
        )
    return None


def parse_rest_bar(
    *,
    symbol: str,
    payload: JsonObject,
    available_at: datetime,
    raw_object_uri: str,
    request_id: str | None,
    timeframe: str = TIMEFRAME,
    adjustment: str = "raw",
) -> MarketBar:
    if adjustment not in {"raw", "split", "dividend", "all"}:
        raise ValueError(f"Unsupported Alpaca adjustment: {adjustment}")
    enriched = {
        "S": symbol,
        "_adjustment": adjustment,
        "_dataset_version": f"alpaca_iex_adjusted_{adjustment}_v1",
        **payload,
    }
    return parse_bar(
        enriched,
        available_at=available_at,
        raw_object_uri=raw_object_uri,
        source_kind=MarketDataSourceKind.REST_BACKFILL,
        request_id=request_id,
        timeframe=timeframe,
    )


def parse_bar(
    payload: JsonObject,
    *,
    available_at: datetime,
    raw_object_uri: str,
    source_kind: MarketDataSourceKind,
    request_id: str | None = None,
    timeframe: str = TIMEFRAME,
) -> MarketBar:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"Unsupported Alpaca timeframe: {timeframe}")
    safe_payload = _safe_payload(payload)
    payload_hash = canonical_hash(safe_payload)
    provider_timestamp = _required_string(payload, "t")
    symbol = _required_string(payload, "S")
    received = require_aware_utc(available_at, "available_at")
    return MarketBar(
        bar_id=stable_id(
            "mbar",
            PROVIDER,
            FEED,
            symbol,
            timeframe,
            provider_timestamp,
            payload_hash,
        ),
        provider=PROVIDER,
        feed=FEED,
        symbol=symbol,
        timeframe=timeframe,
        event_time=_timestamp(provider_timestamp),
        provider_timestamp=provider_timestamp,
        available_at=received,
        ingested_at=received,
        source_kind=source_kind,
        open=_decimal(payload, "o"),
        high=_decimal(payload, "h"),
        low=_decimal(payload, "l"),
        close=_decimal(payload, "c"),
        volume=_decimal(payload, "v"),
        vwap=_optional_decimal(payload.get("vw")),
        trade_count=_integer(payload, "n"),
        request_id=request_id,
        payload_hash=payload_hash,
        raw_object_uri=raw_object_uri,
        payload=safe_payload,
    )


def parse_quote(
    payload: JsonObject,
    *,
    symbol: str,
    available_at: datetime,
    raw_object_uri: str,
    source_kind: MarketDataSourceKind,
) -> MarketQuote:
    safe_payload = _safe_payload(payload)
    payload_hash = canonical_hash(safe_payload)
    provider_timestamp = _required_string(payload, "t")
    received = require_aware_utc(available_at, "available_at")
    return MarketQuote(
        quote_id=stable_id(
            "mquote",
            PROVIDER,
            FEED,
            symbol,
            provider_timestamp,
            payload_hash,
        ),
        provider=PROVIDER,
        feed=FEED,
        symbol=symbol,
        event_time=_timestamp(provider_timestamp),
        provider_timestamp=provider_timestamp,
        available_at=received,
        ingested_at=received,
        source_kind=source_kind,
        bid_exchange=_optional_string(payload.get("bx")),
        bid_price=_decimal(payload, "bp"),
        bid_size_round_lots=_integer(payload, "bs"),
        ask_exchange=_optional_string(payload.get("ax")),
        ask_price=_decimal(payload, "ap"),
        ask_size_round_lots=_integer(payload, "as"),
        conditions=_string_list(payload.get("c")),
        tape=_optional_string(payload.get("z")),
        payload_hash=payload_hash,
        raw_object_uri=raw_object_uri,
        payload=safe_payload,
    )


def parse_trade(
    payload: JsonObject,
    *,
    symbol: str,
    available_at: datetime,
    raw_object_uri: str,
    source_kind: MarketDataSourceKind,
) -> MarketTradeEvent:
    message_type = payload.get("T", "t")
    event_kind = {
        "t": MarketTradeEventKind.TRADE,
        "c": MarketTradeEventKind.CORRECTION,
        "x": MarketTradeEventKind.CANCEL_ERROR,
    }.get(str(message_type), MarketTradeEventKind.TRADE)
    safe_payload = _safe_payload(payload)
    payload_hash = canonical_hash(safe_payload)
    provider_timestamp = _required_string(payload, "t")
    received = require_aware_utc(available_at, "available_at")
    event_id = payload.get("i", payload.get("ci", payload.get("oi")))
    price = payload.get("p", payload.get("cp", payload.get("op")))
    size = payload.get("s", payload.get("cs", payload.get("os")))
    return MarketTradeEvent(
        trade_event_id=stable_id(
            "mtrade",
            PROVIDER,
            FEED,
            symbol,
            event_kind,
            provider_timestamp,
            None if event_id is None else str(event_id),
            payload_hash,
        ),
        provider=PROVIDER,
        feed=FEED,
        symbol=symbol,
        event_kind=event_kind,
        provider_event_id=None if event_id is None else str(event_id),
        event_time=_timestamp(provider_timestamp),
        provider_timestamp=provider_timestamp,
        available_at=received,
        ingested_at=received,
        source_kind=source_kind,
        exchange=_optional_string(payload.get("x")),
        price=_optional_decimal(price),
        size=_optional_decimal(size),
        conditions=_string_list(payload.get("c")),
        tape=_optional_string(payload.get("z")),
        payload_hash=payload_hash,
        raw_object_uri=raw_object_uri,
        payload=safe_payload,
    )


def _default_connection_factory(url: str) -> AbstractAsyncContextManager[WebSocketLike]:
    return cast(
        AbstractAsyncContextManager[WebSocketLike],
        websockets.connect(url, open_timeout=10, close_timeout=5),
    )


async def _receive_messages(
    socket: WebSocketLike,
    *,
    timeout_seconds: int,
) -> list[JsonObject]:
    async with asyncio.timeout(timeout_seconds):
        raw = await socket.recv()
    raw_text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    messages = _decode_message_array(raw_text)
    _raise_stream_error(messages)
    return messages


def _decode_message_array(raw_text: str) -> list[JsonObject]:
    try:
        decoded: object = json.loads(raw_text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise AlpacaStreamError("INVALID_JSON", "Stream frame is not valid JSON") from exc
    if not isinstance(decoded, list):
        raise AlpacaStreamError("INVALID_SHAPE", "Stream frame root must be an array")
    return [_object(item) for item in cast(list[object], decoded)]


def _expect_success(messages: list[JsonObject], expected: str) -> None:
    if not any(
        item.get("T") == "success" and item.get("msg") == expected
        for item in messages
    ):
        raise AlpacaStreamError("PROTOCOL", f"Expected success message {expected!r}")


def _expect_subscription(
    messages: list[JsonObject],
    requested: dict[str, tuple[str, ...]],
) -> None:
    subscription = next(
        (item for item in messages if item.get("T") == "subscription"),
        None,
    )
    if subscription is None:
        raise AlpacaStreamError("PROTOCOL", "Missing subscription acknowledgement")
    for channel in ("trades", "quotes", "bars", "updatedBars"):
        acknowledged = subscription.get(channel)
        if not requested[channel] and acknowledged is None:
            continue
        if not isinstance(acknowledged, list):
            raise AlpacaStreamError("SUBSCRIPTION", f"Incomplete {channel} subscription")
        acknowledged_items = cast(list[object], acknowledged)
        if not set(requested[channel]).issubset(
            {str(item) for item in acknowledged_items}
        ):
            raise AlpacaStreamError("SUBSCRIPTION", f"Incomplete {channel} subscription")


def _raise_stream_error(messages: list[JsonObject]) -> None:
    error = next((item for item in messages if item.get("T") == "error"), None)
    if error is not None:
        code = error.get("code", "UNKNOWN")
        safe_code = code if isinstance(code, (int, str)) else str(code)
        raise AlpacaStreamError(safe_code, str(error.get("msg", "error")))


def _safe_http_detail(raw: str) -> str:
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError:
        return "provider returned an error"
    if isinstance(decoded, dict):
        payload = cast(dict[str, object], decoded)
        return str(payload.get("message", payload.get("error", "provider returned an error")))
    return "provider returned an error"


def _safe_payload(value: JsonObject) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], _json_safe(value))


def _json_safe(value: object) -> JsonValue:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {str(key): _json_safe(item) for key, item in mapping.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in cast(list[object], value)]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError("Alpaca payload item must be an object")
    return cast(JsonObject, value)


def _required_string(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Alpaca payload requires string field {key}")
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _decimal(payload: JsonObject, key: str) -> Decimal:
    value = _optional_decimal(payload.get(key))
    if value is None:
        raise ValueError(f"Alpaca payload requires numeric field {key}")
    return value


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Boolean is not a market-data numeric value")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _integer(payload: JsonObject, key: str) -> int:
    value = payload.get(key)
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"Alpaca payload requires integer field {key}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Alpaca payload requires integer field {key}") from exc


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Alpaca condition field must be an array")
    return [str(item) for item in cast(list[object], value)]


def _timestamp(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid Alpaca timestamp: {raw}") from exc
    return require_aware_utc(parsed, "provider timestamp")


def _rfc3339(value: datetime) -> str:
    return require_aware_utc(value).isoformat().replace("+00:00", "Z")
