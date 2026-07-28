from __future__ import annotations

import asyncio
import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

import httpx

from trading.data.alpaca import (
    AlpacaCredentials,
    AlpacaRestClient,
    AlpacaStreamClient,
    StreamFrame,
    parse_stream_message,
)
from trading.data.raw_store import ImmutableRawStore
from trading.domain.contracts import MarketBar, MarketQuote, MarketTradeEvent
from trading.domain.enums import MarketDataSourceKind
from trading.domain.time import FrozenClock


def test_stream_parsers_preserve_native_fields_and_nanosecond_timestamp() -> None:
    received = datetime(2026, 7, 27, 14, 31, tzinfo=UTC)
    bar = parse_stream_message(
        {
            "T": "u",
            "S": "SOXL",
            "o": 42.1,
            "h": 42.4,
            "l": 42.0,
            "c": 42.3,
            "v": 1200,
            "vw": 42.22,
            "n": 73,
            "t": "2026-07-27T14:30:00.123456789Z",
        },
        available_at=received,
        raw_object_uri="raw://bar",
    )
    assert isinstance(bar, MarketBar)
    assert bar.source_kind is MarketDataSourceKind.STREAM_UPDATE
    assert bar.provider_timestamp == "2026-07-27T14:30:00.123456789Z"
    assert str(bar.high) == "42.4"

    quote = parse_stream_message(
        {
            "T": "q",
            "S": "SOXL",
            "bx": "V",
            "bp": 42.29,
            "bs": 3,
            "ax": "V",
            "ap": 42.31,
            "as": 5,
            "c": ["R"],
            "t": "2026-07-27T14:30:59.900000001Z",
            "z": "C",
        },
        available_at=received,
        raw_object_uri="raw://quote",
    )
    assert isinstance(quote, MarketQuote)
    assert quote.bid_size_round_lots == 3
    assert quote.ask_size_round_lots == 5

    trade = parse_stream_message(
        {
            "T": "t",
            "S": "SOXL",
            "i": 991,
            "x": "V",
            "p": 42.3,
            "s": 100,
            "c": [],
            "t": "2026-07-27T14:30:59.950000001Z",
            "z": "C",
        },
        available_at=received,
        raw_object_uri="raw://trade",
    )
    assert isinstance(trade, MarketTradeEvent)
    assert trade.provider_event_id == "991"


def test_rest_bars_exhaust_page_tokens_and_store_raw_pages(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["APCA-API-KEY-ID"] == "test-key"
        assert request.url.params["feed"] == "iex"
        page_token = request.url.params.get("page_token")
        minute = "30" if page_token is None else "31"
        payload = {
            "bars": {
                "SOXL": [
                    {
                        "t": f"2026-07-27T14:{minute}:00Z",
                        "o": 42.1,
                        "h": 42.4,
                        "l": 42.0,
                        "c": 42.3,
                        "v": 1200,
                        "vw": 42.22,
                        "n": 73,
                    }
                ]
            },
            "next_page_token": "page-2" if page_token is None else None,
        }
        return httpx.Response(
            200,
            json=payload,
            headers={"x-request-id": f"req-{len(calls)}"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rest = AlpacaRestClient(
        credentials=AlpacaCredentials("test-key", "test-secret"),
        raw_store=ImmutableRawStore(tmp_path),
        clock=FrozenClock(datetime(2026, 7, 27, 15, 0, tzinfo=UTC)),
        client=client,
    )
    async def run() -> list[MarketBar]:
        bars = await rest.fetch_bars(
            symbols=("SOXL",),
            start=datetime(2026, 7, 27, 14, 0, tzinfo=UTC),
            end=datetime(2026, 7, 27, 15, 0, tzinfo=UTC),
        )
        await client.aclose()
        return bars

    bars = asyncio.run(run())

    assert len(calls) == 2
    assert calls[1].url.params["page_token"] == "page-2"
    assert [bar.request_id for bar in bars] == ["req-1", "req-2"]
    assert len(list(tmp_path.rglob("*.json"))) == 2


def test_websocket_authenticates_and_confirms_all_subscriptions(tmp_path: Path) -> None:
    socket = FakeSocket(
        [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
            (
                '[{"T":"subscription","trades":["SOXL"],"quotes":["SOXL"],'
                '"bars":["SOXL"],"updatedBars":["SOXL"]}]'
            ),
            (
                '[{"T":"q","S":"SOXL","bx":"V","bp":42.29,"bs":3,'
                '"ax":"V","ap":42.31,"as":5,"c":[],"t":'
                '"2026-07-27T14:30:59Z","z":"C"}]'
            ),
        ]
    )
    stream = AlpacaStreamClient(
        credentials=AlpacaCredentials("test-key", "test-secret"),
        symbols=("SOXL",),
        raw_store=ImmutableRawStore(tmp_path),
        clock=FrozenClock(datetime(2026, 7, 27, 14, 31, tzinfo=UTC)),
        connection_factory=lambda _url: FakeConnection(socket),
    )
    async def run() -> tuple[StreamFrame, StreamFrame]:
        frames = stream.frames()
        connected = await anext(frames)
        data = await anext(frames)
        await frames.aclose()
        return connected, data

    connected, data = asyncio.run(run())

    assert connected.connected is True
    assert isinstance(data, StreamFrame)
    assert data.connected is False
    assert data.messages[0]["T"] == "q"
    assert json.loads(socket.sent[0])["action"] == "auth"
    assert json.loads(socket.sent[1])["updatedBars"] == ["SOXL"]


def test_websocket_accepts_channel_specific_basic_plan(tmp_path: Path) -> None:
    socket = FakeSocket(
        [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
            (
                '[{"T":"subscription","quotes":["SOXL"],'
                '"bars":["SOXL","SOXS"]}]'
            ),
            (
                '[{"T":"q","S":"SOXL","bx":"V","bp":42.29,"bs":3,'
                '"ax":"V","ap":42.31,"as":5,"c":[],"t":'
                '"2026-07-27T14:30:59Z","z":"C"}]'
            ),
        ]
    )
    stream = AlpacaStreamClient(
        credentials=AlpacaCredentials("test-key", "test-secret"),
        symbols=("SOXL", "SOXS"),
        trade_symbols=(),
        quote_symbols=("SOXL",),
        bar_symbols=("SOXL", "SOXS"),
        updated_bar_symbols=(),
        raw_store=ImmutableRawStore(tmp_path),
        clock=FrozenClock(datetime(2026, 7, 27, 14, 31, tzinfo=UTC)),
        connection_factory=lambda _url: FakeConnection(socket),
    )

    async def run() -> StreamFrame:
        frames = stream.frames()
        await anext(frames)
        data = await anext(frames)
        await frames.aclose()
        return data

    data = asyncio.run(run())

    assert data.messages[0]["T"] == "q"
    subscription = json.loads(socket.sent[1])
    assert subscription == {
        "action": "subscribe",
        "trades": [],
        "quotes": ["SOXL"],
        "bars": ["SOXL", "SOXS"],
        "updatedBars": [],
    }


class FakeSocket:
    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        return self._responses.pop(0)


class FakeConnection(AbstractAsyncContextManager[FakeSocket]):
    def __init__(self, socket: FakeSocket) -> None:
        self._socket = socket

    async def __aenter__(self) -> FakeSocket:
        return self._socket

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None
